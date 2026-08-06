import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from . import ai, agentes, auth, bling, catalogo, decisao, kpis, nfe, observ, precificacao, pricing, qualidade, radar, scraper, shopee, shopee_boost, shopee_boost_auto, shopee_campanhas, shopee_impressao, shopee_promo_agentes, shopee_promo_auto, shopee_promo_painel, shopee_reviews, webhooks
from .config import settings
from .db import run_migrations, SessionLocal, Base, engine, garantir_colunas_extras
from .models import NfeConfig, User, WebhookEvento


async def _agendador_radar():
    """Laço em segundo plano: varre os alvos de todos os tenants a cada N horas."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(max(settings.radar_intervalo_horas, 1) * 3600)
        try:
            await loop.run_in_executor(None, radar.varrer_todos)
        except Exception:  # noqa: BLE001 — o agendador nunca derruba o app
            pass


async def _agendador_boost():
    """Laço do auto-boost da Shopee: a cada 10min checa quem tem vaga e impulsiona
    os próximos da fila (reimpulsiona quando os 4h de um lote acabam). Quando a
    auto-seleção está ligada, reabastece a fila automática periodicamente."""
    loop = asyncio.get_event_loop()
    ciclos = 0
    while True:
        await asyncio.sleep(600)  # 10 minutos
        ciclos += 1
        try:
            from .models import ShopeeBoostConfig, ShopeeBoostItem
            db = SessionLocal()
            try:
                cfgs = db.query(ShopeeBoostConfig).filter_by(ativo=True).all()
                ativos = [c.user_id for c in cfgs]
                cond = [c.user_id for c in cfgs if getattr(c, "cond_ativo", False)]
                # quem precisa reabastecer a fila automática (a cada ~12h ou se esvaziou)
                reabastecer = []
                for c in cfgs:
                    if not getattr(c, "auto_selecao", False):
                        continue
                    n_auto = (db.query(ShopeeBoostItem)
                              .filter_by(user_id=c.user_id, auto=True).count())
                    if n_auto == 0 or ciclos % 72 == 0:
                        reabastecer.append(c.user_id)
            finally:
                db.close()
            for uid in reabastecer:
                await loop.run_in_executor(None, shopee_boost.auto_selecionar, uid, None)
            for uid in cond:  # boost condicional: fixa ameaçados + roda ciclo
                await loop.run_in_executor(None, shopee_boost_auto.aplicar, uid)
            for uid in ativos:
                if uid in cond:
                    continue  # aplicar já rodou o ciclo deste usuário
                await loop.run_in_executor(None, shopee_boost.ciclo, uid)
        except Exception:  # noqa: BLE001 — nunca derruba o app
            pass


async def _agendador_catalogo():
    """Rede de segurança: re-sincroniza o catálogo de quem já sincronizou ao menos uma vez,
    a cada N horas (além do webhook, caso algum evento tenha se perdido)."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(max(settings.catalogo_resync_horas, 1) * 3600)
        try:
            from .models import CatalogoSync
            db = SessionLocal()
            try:
                usuarios = [c.user_id for c in db.query(CatalogoSync).all()]
            finally:
                db.close()
            for uid in usuarios:
                await loop.run_in_executor(None, catalogo.sincronizar_tudo, uid)
        except Exception:  # noqa: BLE001
            pass


async def _agendador_reviews():
    """Modo automático das avaliações: a cada hora responde sozinho as notas
    configuradas (ex.: 4 e 5 estrelas), deixando notas baixas para revisão manual."""
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(3600)  # 1 hora
        try:
            from .models import ShopeeReviewConfig
            db = SessionLocal()
            try:
                autos = [c.user_id for c in db.query(ShopeeReviewConfig).filter_by(modo="auto").all()]
            finally:
                db.close()
            for uid in autos:
                await loop.run_in_executor(None, shopee_reviews.auto_responder, uid)
        except Exception:  # noqa: BLE001 — nunca derruba o app
            pass


async def _agendador_promo():
    """Motor de promoções: roda logo após subir e a cada 30 min. Tira foto das vendas
    a cada ~6h (para detectar queda) e roda o ciclo de quem está em modo automático
    (o auto_ciclo respeita o intervalo configurado internamente)."""
    loop = asyncio.get_event_loop()
    await asyncio.sleep(120)  # primeira passada ~2min após o boot (não espera 6h)
    ticks = 0
    while True:
        try:
            from .models import ShopeePromoConfig, ShopeeConta
            db = SessionLocal()
            try:
                lojas = [c.user_id for c in db.query(ShopeeConta).all()]
                autos = [c.user_id for c in db.query(ShopeePromoConfig)
                         .filter_by(ativo=True, modo="auto").all()]
            finally:
                db.close()
            if ticks % 12 == 0:  # snapshot de vendas ~a cada 6h
                for uid in lojas:
                    await loop.run_in_executor(None, shopee_promo_auto.snapshot_vendas, uid)
            for uid in autos:  # auto_ciclo respeita o intervalo internamente
                await loop.run_in_executor(None, shopee_promo_auto.auto_ciclo, uid)
            for uid in autos:  # agentes por vendas: o radar respeita o intervalo do PAINEL (verificacao_horas)
                try:
                    await loop.run_in_executor(None, shopee_promo_agentes.ciclo, uid)
                except Exception:  # noqa: BLE001
                    pass
            # --- Agentes do Mercado Livre em modo automático (piso-safe, teto, respeita intervalo) ---
            try:
                from .models import AgenteConfig
                db = SessionLocal()
                try:
                    pend = db.query(AgenteConfig).filter(
                        AgenteConfig.automatico.is_(True), AgenteConfig.kill_switch.is_(False)).all()
                    agora_ml = datetime.utcnow()
                    alvos_ml = [c.user_id for c in pend
                                if (c.ultima_execucao_auto is None
                                    or (agora_ml - c.ultima_execucao_auto).total_seconds()
                                    >= (c.intervalo_horas or 6) * 3600)]
                finally:
                    db.close()
                for uid in alvos_ml:
                    await loop.run_in_executor(None, _rodar_agentes, uid, "auto")
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001 — nunca derruba o app
            pass
        ticks += 1
        await asyncio.sleep(1800)  # 30 min


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        observ.configurar_logs()
        observ.instrumentar()
    except Exception:  # noqa: BLE001 — observabilidade NUNCA pode impedir o boot
        pass
    print("[precifica] backend v5.8 — boot OK · consulta e download da prova · leitura do banco + varredura de fundo", flush=True)
    run_migrations()
    # garante tabelas aditivas — não mexe nas existentes
    # Cria TODAS as tabelas faltantes (checkfirst não toca nas que já existem). Robusto:
    # antes uma lista fixa engolia erros e podia pular tabelas novas (ex.: shopee_sync).
    try:
        from . import models as _modelos  # noqa: F401 — registra todos os modelos no metadata
        Base.metadata.create_all(bind=engine)
    except Exception:  # noqa: BLE001
        pass
    # coluna aditiva: raw no ml_pedido_cache (fusão com o cache de análise de vendas)
    try:
        from sqlalchemy import text as _sqltext
        with engine.connect() as _cx:
            _cx.execute(_sqltext("ALTER TABLE ml_pedido_cache ADD COLUMN IF NOT EXISTS raw JSON"))
            _cx.commit()
    except Exception:  # noqa: BLE001
        pass
    # Rede de segurança: cada tabela nova em seu próprio try — uma falha não trava as outras.
    try:
        from .models import (ProdutoCache, CatalogoSync, ProdutoPrecoSnapshot,
                             ShopeeItemCache, ShopeeSync, VinculosSync, Notificacao)
        for M in (ProdutoCache, CatalogoSync, ProdutoPrecoSnapshot,
                  ShopeeItemCache, ShopeeSync, VinculosSync, Notificacao):
            try:
                M.__table__.create(bind=engine, checkfirst=True)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    garantir_colunas_extras()  # colunas novas em tabelas já existentes (auto-seleção do boost)
    tarefa = asyncio.create_task(_agendador_radar()) if settings.radar_intervalo_horas > 0 else None
    tarefa_boost = asyncio.create_task(_agendador_boost())
    tarefa_reviews = asyncio.create_task(_agendador_reviews())
    tarefa_promo = asyncio.create_task(_agendador_promo())
    tarefa_cat = asyncio.create_task(_agendador_catalogo()) if settings.catalogo_resync_horas > 0 else None
    yield
    if tarefa:
        tarefa.cancel()
    tarefa_boost.cancel()
    tarefa_reviews.cancel()
    tarefa_promo.cancel()
    if tarefa_cat:
        tarefa_cat.cancel()


app = FastAPI(title="BlingAI Manager — Backend", version="0.2.0", lifespan=lifespan)

# Ativa a observabilidade já no import, além do lifespan — assim os logs aparecem no
# Railway mesmo que algo no startup engasgue. Nunca fatal.
try:
    observ.configurar_logs()
    observ.instrumentar()
except Exception:  # noqa: BLE001
    pass


def _brl(v) -> str:
    """Formata um número como moeda BR para textos de notificação."""
    try:
        return "R$ " + f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "R$ 0,00"

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.frontend_origin == "*" else [settings.frontend_origin],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _observabilidade(request: Request, call_next):
    return await observ.middleware(request, call_next)


@app.exception_handler(Exception)
async def _erro_global(request: Request, exc: Exception):
    """Qualquer erro inesperado volta como JSON legível COM cabeçalho CORS, em vez do
    net::ERR_FAILED opaco que o navegador mostra quando um 500 vem sem CORS."""
    observ.log_excecao(request, exc)
    origem = request.headers.get("origin")
    headers = {"Access-Control-Allow-Origin": origem or "*",
               "Access-Control-Allow-Credentials": "true"}
    return JSONResponse(status_code=500,
                        content={"detail": f"Erro interno: {type(exc).__name__}: {exc}"},
                        headers=headers)


# ------------------------------- Modelos ---------------------------------- #
class RegistroIn(BaseModel):
    email: str
    senha: str
    nome: str | None = None


class LoginIn(BaseModel):
    email: str
    senha: str


@app.get("/health")
def health():
    return {"ok": True}


# ------------------------------- Auth ------------------------------------- #
@app.post("/auth/register")
def register(body: RegistroIn):
    user = auth.registrar(body.email, body.senha, body.nome)
    return {"id": user.id, "email": user.email, "token": auth.create_access_token(user.id)}


@app.post("/auth/login")
def login(body: LoginIn):
    user = auth.autenticar(body.email, body.senha)
    return {"id": user.id, "email": user.email, "token": auth.create_access_token(user.id)}


@app.get("/auth/me")
def me(user: User = Depends(auth.get_current_user)):
    return {"id": user.id, "email": user.email, "nome": user.nome}


# --------------------------- OAuth Bling (por tenant) --------------------- #
@app.get("/auth/bling/login")
def bling_login(user: User = Depends(auth.get_current_user)):
    # Retorna a URL para o front redirecionar (o user_id viaja assinado no state).
    return {"url": bling.get_authorize_url(user.id)}


@app.get("/auth/bling/callback")
def bling_callback(code: str = Query(...), state: str = Query(None)):
    try:
        uid, st = bling.resolver_state(state)   # casa o exato ou a única pendente
        bling.exchange_code(uid, code)          # se falhar, o state segue p/ retry
        bling.consume_state(st)                 # consome só no sucesso
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "mensagem": "Conta Bling autorizada com sucesso."}


@app.get("/auth/bling/status")
def bling_status(user: User = Depends(auth.get_current_user)):
    return bling.token_status(user.id)


# ------------------------------- Webhooks --------------------------------- #
def _processar_evento_async(user_id: int, evento_db_id: int):
    """Processa o evento fora do ciclo da resposta (fila em background)."""
    db = SessionLocal()
    try:
        reg = db.query(WebhookEvento).filter_by(id=evento_db_id).first()
        if not reg or reg.processado:
            return
        data = (reg.payload or {}).get("data") or {}
        recurso = (reg.recurso or "").lower()
        webhooks.processar(user_id, reg.recurso, reg.acao, data)
        # NF-e: aplica o desconto padrão automaticamente quando o modo auto está ligado.
        # Roda aqui (background) porque envolve buscar a nota + PUT de volta no Bling.
        if recurso in ("nfe", "notafiscal", "nota_fiscal", "notafiscaleletronica") and reg.entidade_id:
            cfg_nfe = db.query(NfeConfig).filter_by(user_id=user_id).first()
            try:
                reg.resultado = nfe.processar_evento(user_id, reg.entidade_id, cfg_nfe)
            except Exception:  # noqa: BLE001
                reg.resultado = {"id": str(reg.entidade_id), "ok": False, "motivo": "erro_inesperado"}
        # Mantém o cache do catálogo atualizado a partir do push
        if recurso in ("produto", "produtos") and reg.entidade_id:
            if (reg.acao or "").lower() in ("deleted", "deletado", "excluido"):
                catalogo.remover_produto(db, user_id, reg.entidade_id)
            else:
                catalogo.atualizar_do_bling(user_id, reg.entidade_id)
            webhooks.confirmar_sync(db, user_id, reg.entidade_id)
        reg.processado = True
        db.commit()
    except Exception:  # noqa: BLE001 — background nunca quebra o app
        pass
    finally:
        db.close()


@app.post("/webhooks/bling/{token}")
async def receber_webhook(token: str, request: Request, background_tasks: BackgroundTasks):
    """Recebe os eventos do Bling (push). Responde 200 NA HORA e joga TODO o trabalho
    de banco/rede para o threadpool (background) — nada de I/O síncrono no event loop,
    senão o Bling (que dispara muitos webhooks) congela o servidor inteiro."""
    user_id = webhooks.verificar_token(token)
    if not user_id:
        return {"ok": False}
    try:
        corpo = await request.json()
    except Exception:  # noqa: BLE001
        corpo = {}
    background_tasks.add_task(_registrar_e_processar, user_id, corpo)
    return {"ok": True}


def _registrar_e_processar(user_id: int, corpo: dict):
    """Roda no threadpool (fora do event loop): grava o evento e processa em seguida."""
    reg_id = None
    db = SessionLocal()
    try:
        reg = webhooks.registrar_evento(db, user_id, corpo)  # gravação rápida + dedupe
        reg_id = reg.id if reg else None
    except Exception:  # noqa: BLE001
        pass
    finally:
        db.close()
    if reg_id:
        _processar_evento_async(user_id, reg_id)


@app.get("/api/webhooks/url")
def webhook_url(request: Request, user: User = Depends(auth.get_current_user)):
    """URL única e assinada deste tenant para colar no Bling (Configuração de servidores)."""
    base = str(request.base_url).rstrip("/")
    token = webhooks.gerar_token(user.id)
    return {"url": f"{base}/webhooks/bling/{token}",
            "recursos": ["produtos", "pedidos de vendas", "notas fiscais eletrônicas", "estoque"]}


@app.get("/api/webhooks/eventos")
def webhook_eventos(limite: int = 30, user: User = Depends(auth.get_current_user)):
    """Últimos eventos recebidos do Bling (pra conferir que está chegando)."""
    db = SessionLocal()
    try:
        q = (db.query(WebhookEvento).filter_by(user_id=user.id)
             .order_by(WebhookEvento.recebido_em.desc()).limit(min(limite, 100)).all())
        return {"eventos": [{
            "event": e.event, "recurso": e.recurso, "acao": e.acao,
            "entidade_id": e.entidade_id, "processado": e.processado,
            "recebido_em": e.recebido_em.isoformat() if e.recebido_em else None,
        } for e in q]}
    finally:
        db.close()


# ------------------------------- Catálogo (cache) ------------------------- #
@app.post("/api/catalogo/sincronizar")
def catalogo_sincronizar(background_tasks: BackgroundTasks, user: User = Depends(auth.get_current_user)):
    """Dispara a sincronização COMPLETA do catálogo (puxa tudo do Bling pro cache).
    Roda em background; acompanhe por /api/catalogo/sync_status."""
    est = catalogo.status(user.id)
    if est["status"] == "rodando":
        return {"ok": True, "ja_rodando": True, **est}
    background_tasks.add_task(catalogo.sincronizar_tudo, user.id)
    return {"ok": True, "iniciado": True}


@app.get("/api/catalogo/sync_status")
def catalogo_sync_status(user: User = Depends(auth.get_current_user)):
    return catalogo.status(user.id)


@app.post("/api/catalogo/vinculos/enriquecer")
def catalogo_vinculos_enriquecer(background_tasks: BackgroundTasks, user: User = Depends(auth.get_current_user)):
    """Dispara o mapeamento de canais por produto (lê os vínculos no Bling, ~1 chamada por
    produto). Roda em background; acompanhe por /api/catalogo/vinculos/status."""
    st = catalogo.status_vinculos(user.id)
    if st.get("status") == "rodando":
        return {"ok": True, "ja_rodando": True, **st}
    background_tasks.add_task(catalogo.enriquecer_vinculos, user.id)
    return {"ok": True, "iniciado": True}


@app.get("/api/catalogo/vinculos/status")
def catalogo_vinculos_status(user: User = Depends(auth.get_current_user)):
    return catalogo.status_vinculos(user.id)


@app.post("/api/shopee/catalogo/sincronizar")
def shopee_catalogo_sincronizar(background_tasks: BackgroundTasks, user: User = Depends(auth.get_current_user)):
    """Dispara a sincronização do catálogo da Shopee pro cache (item_id, sku, preço, promoção).
    Roda em background; acompanhe por /api/shopee/catalogo/status."""
    from . import shopee_catalogo
    est = shopee_catalogo.status(user.id)
    if est["status"] == "rodando":
        return {"ok": True, "ja_rodando": True, **est}
    background_tasks.add_task(shopee_catalogo.sincronizar, user.id)
    return {"ok": True, "iniciado": True}


@app.get("/api/shopee/catalogo/status")
def shopee_catalogo_status(user: User = Depends(auth.get_current_user)):
    from . import shopee_catalogo
    return shopee_catalogo.status(user.id)


@app.get("/api/shopee/item")
def shopee_item_lookup(sku: str, user: User = Depends(auth.get_current_user)):
    """Anúncio da Shopee (preço, preço original, promoção) por SKU — lê do cache."""
    from . import shopee_catalogo
    return shopee_catalogo.item_por_sku(user.id, sku) or {"encontrado": False}


@app.get("/api/catalogo")
def catalogo_listar(busca: str = "", pagina: int = 1, limite: int = 50, situacao: str = "",
                    user: User = Depends(auth.get_current_user)):
    """Lê o catálogo DO CACHE local (rápido, sem martelar o Bling)."""
    return catalogo.listar(user.id, busca=busca, pagina=pagina, limite=min(limite, 200), situacao=situacao)


@app.get("/api/shopee/diagnostico")
def shopee_diagnostico(user: User = Depends(auth.get_current_user)):
    """Testa cada chamada da Shopee individualmente e reporta ok/erro por funcionalidade.
    Serve para descobrir exatamente o que não funciona (e o porquê), sem adivinhação."""
    import time as _t
    if not shopee.app_configurado():
        return {"app": False, "conectado": False, "testes": [],
                "resumo": "App Shopee não configurado no servidor (PARTNER_ID/PARTNER_KEY)."}
    testes = []

    def probe(nome, path, extra=None, metodo="GET"):
        try:
            d = shopee._chamar(user.id, path, extra=extra, metodo=metodo, timeout=8)
            err = d.get("error") if isinstance(d, dict) else None
            if err:
                testes.append({"nome": nome, "ok": False, "erro": f"{err}: {d.get('message') or ''}".strip(": ")})
            else:
                resp = d.get("response") or {}
                qtd = next((len(resp[k]) for k in ("item", "item_comment_list", "question_list",
                            "order_list", "discount_list", "return_list")
                            if isinstance(resp.get(k), list)), None)
                testes.append({"nome": nome, "ok": True, "qtd": qtd})
        except shopee.ShopeeError as e:
            testes.append({"nome": nome, "ok": False, "erro": str(e)})
        except Exception as e:  # noqa: BLE001
            testes.append({"nome": nome, "ok": False, "erro": f"{type(e).__name__}: {e}"})

    agora = int(_t.time())
    probe("Conexão e dados da loja", "/api/v2/shop/get_shop_info")
    probe("Produtos (catálogo Shopee)", "/api/v2/product/get_item_list",
          {"offset": 0, "page_size": 3, "item_status": "NORMAL"})
    probe("Avaliações (reviews)", "/api/v2/product/get_comment", {"page_size": 5})
    probe("Perguntas (Q&A)", "/api/v2/sip/get_item_qa_list", {"page_no": 1, "page_size": 5})
    probe("Pedidos", "/api/v2/order/get_order_list",
          {"time_range_field": "create_time", "time_from": agora - 7 * 86400,
           "time_to": agora, "page_size": 5})
    probe("Descontos / promoções", "/api/v2/discount/get_discount_list",
          {"discount_status": "ongoing", "page_size": 5})
    probe("Saúde da loja", "/api/v2/account_health/get_shop_performance")
    probe("Devoluções", "/api/v2/returns/get_return_list", {"page_no": 0, "page_size": 5})
    ok = sum(1 for t in testes if t["ok"])
    return {"app": True, "conectado": testes[0]["ok"], "ok": ok, "total": len(testes), "testes": testes}


# -------------------------------- Shopee ---------------------------------- #
@app.get("/api/shopee/status")
def shopee_status(user: User = Depends(auth.get_current_user)):
    """Estado da conexão (app + loja) e validação com os dados da loja."""
    est = shopee.status_conexao(user.id)
    if not est.get("loja"):
        return {"configurada": False, "ok": False, **est}
    try:
        info = shopee.info_loja(user.id)
        return {"configurada": True, "ok": True, "loja": info.get("response") or info, **est}
    except shopee.ShopeeError as e:
        return {"configurada": True, "ok": False, "erro": str(e), **est}


@app.post("/api/shopee/conectar")
def shopee_conectar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Salva as credenciais da loja (shop_id + access_token + refresh_token).
    Use quando você já tem os tokens em mãos (sem passar pelo fluxo de OAuth)."""
    shop_id = payload.get("shop_id"); at = payload.get("access_token")
    if not shop_id or not at:
        raise HTTPException(status_code=422, detail="Informe shop_id e access_token.")
    shopee.salvar_conta(user.id, shop_id, at, payload.get("refresh_token"),
                        int(payload.get("expire_in", 14400)))
    return {"ok": True}


@app.post("/api/shopee/renovar")
def shopee_renovar(user: User = Depends(auth.get_current_user)):
    try:
        return {"ok": shopee.renovar_token(user.id)}
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


def _shopee_redirect_base(request: Request) -> str:
    """URL pública do backend para o Shopee redirecionar de volta após o login."""
    if settings.shopee_redirect_base:
        base = settings.shopee_redirect_base.rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = "https://" + base
        return base
    base = str(request.base_url).rstrip("/")
    return base.replace("http://", "https://") if "localhost" not in base and "127.0.0.1" not in base else base


@app.get("/api/shopee/auth/login")
def shopee_auth_login(request: Request, user: User = Depends(auth.get_current_user)):
    """Devolve a URL de autorização da Shopee. O front abre num popup; ao autorizar,
    a Shopee redireciona para /api/shopee/auth/callback com o code, e a gente troca por token."""
    if not shopee.app_configurado():
        raise HTTPException(status_code=400, detail="App Shopee não configurado (PARTNER_ID/PARTNER_KEY).")
    state = shopee.state_token(user.id)
    redirect = f"{_shopee_redirect_base(request)}/api/shopee/auth/callback/{state}"
    return {"url": shopee.url_autorizacao(redirect)}


_HTML_OK = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Shopee conectada</title></head>
<body style="font-family:system-ui,sans-serif;text-align:center;padding:3rem;background:#0b0b0f;color:#eee">
<div style="font-size:48px">✅</div>
<h2 style="color:#34C759">Loja Shopee conectada!</h2>
<p style="color:#aaa">Pode fechar esta janela e voltar ao Precifica AI.</p>
<script>try{if(window.opener){window.opener.postMessage('shopee_auth_ok','*');}}catch(e){}setTimeout(function(){window.close();},1500);</script>
</body></html>"""

_HTML_ERR = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Erro</title></head>
<body style="font-family:system-ui,sans-serif;text-align:center;padding:3rem;background:#0b0b0f;color:#eee">
<div style="font-size:48px">⚠️</div><h2 style="color:#ff5a52">Não foi possível conectar</h2>
<p style="color:#aaa">{msg}</p>
<script>try{{if(window.opener){{window.opener.postMessage('shopee_auth_err','*');}}}}catch(e){{}}</script>
</body></html>"""


@app.get("/api/shopee/auth/callback/{state}")
def shopee_auth_callback(state: str, code: str = "", shop_id: str = ""):
    """Callback do OAuth da Shopee (aberto pelo redirect). Troca o code por token e salva."""
    uid = shopee.ler_state(state)
    if not uid:
        return HTMLResponse(_HTML_ERR.format(msg="Sessão de conexão expirada. Tente novamente."), status_code=400)
    if not code:
        return HTMLResponse(_HTML_ERR.format(msg="Autorização não recebida da Shopee."), status_code=400)
    try:
        shopee.trocar_code_por_token(uid, code, shop_id)
        return HTMLResponse(_HTML_OK)
    except shopee.ShopeeError as e:
        return HTMLResponse(_HTML_ERR.format(msg=str(e)), status_code=400)


@app.get("/api/shopee/produtos")
def shopee_produtos(offset: int = 0, limite: int = 50, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_itens(user.id, offset=offset, limite=limite)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/desempenho")
def shopee_desempenho(user: User = Depends(auth.get_current_user)):
    try:
        return shopee.desempenho_loja(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Boost (auto-impulsionamento rotativo) ----
@app.get("/api/shopee/boost/status")
def shopee_boost_status(user: User = Depends(auth.get_current_user)):
    return shopee_boost.status(user.id)


@app.get("/api/shopee/boost/condicional")
def shopee_boost_cond_get(user: User = Depends(auth.get_current_user)):
    """Config + diagnóstico do boost condicional pelo Radar (quem está ameaçado agora)."""
    cfg = shopee_boost_auto.config(user.id)
    try:
        ev = shopee_boost_auto.avaliar(user.id)
    except shopee.ShopeeError as e:
        ev = {"erro": str(e), "ameacados": []}
    return {"config": cfg, **ev}


@app.put("/api/shopee/boost/condicional")
def shopee_boost_cond_put(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    return shopee_boost_auto.salvar_config(user.id, payload)


@app.post("/api/shopee/boost/condicional/aplicar")
def shopee_boost_cond_aplicar(user: User = Depends(auth.get_current_user)):
    """Avalia e aplica agora: fixa os ameaçados em boost e libera os que saíram da mira."""
    try:
        return shopee_boost_auto.aplicar(user.id, forcar=True)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.put("/api/shopee/boost/config")
def shopee_boost_config(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    db = SessionLocal()
    try:
        from .models import ShopeeBoostConfig
        c = db.query(ShopeeBoostConfig).filter_by(user_id=user.id).first()
        if not c:
            c = ShopeeBoostConfig(user_id=user.id); db.add(c)
        if "ativo" in payload: c.ativo = bool(payload["ativo"])
        if "criterio" in payload: c.criterio = payload["criterio"]
        if "janela_inicio" in payload: c.janela_inicio = int(payload["janela_inicio"])
        if "janela_fim" in payload: c.janela_fim = int(payload["janela_fim"])
        if "max_simultaneos" in payload: c.max_simultaneos = min(int(payload["max_simultaneos"]), 5)
        if "auto_selecao" in payload: c.auto_selecao = bool(payload["auto_selecao"])
        if "auto_estrategia" in payload: c.auto_estrategia = payload["auto_estrategia"]
        if "auto_maximo" in payload: c.auto_maximo = max(5, min(int(payload["auto_maximo"]), 50))
        if "janelas" in payload:
            js = payload["janelas"]
            c.janelas = [[int(j[0]), int(j[1])] for j in js if isinstance(j, (list, tuple)) and len(j) == 2] if js else None
        if "cond_ativo" in payload: c.cond_ativo = bool(payload["cond_ativo"])
        if "cond_gatilho_pct" in payload: c.cond_gatilho_pct = float(payload["cond_gatilho_pct"])
        if "cond_max" in payload: c.cond_max = max(1, min(int(payload["cond_max"]), 5))
        if "cond_estoque" in payload: c.cond_estoque = bool(payload["cond_estoque"])
        if "cond_surto" in payload: c.cond_surto = bool(payload["cond_surto"])
        c.atualizado_em = datetime.utcnow()
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.post("/api/shopee/boost/auto_selecionar")
def shopee_boost_auto_selecionar(payload: dict = Body(default={}), user: User = Depends(auth.get_current_user)):
    """Os agentes escolhem os produtos do boost automaticamente, por estratégia."""
    try:
        return shopee_boost.auto_selecionar(user.id, payload.get("estrategia"))
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/boost/itens")
def shopee_boost_add(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Adiciona itens à lista de boost. Body: {itens:[{item_id, nome, fixo?, prioridade?}]}."""
    db = SessionLocal()
    try:
        from .models import ShopeeBoostItem
        add = payload.get("itens", [])
        fixos = db.query(ShopeeBoostItem).filter_by(user_id=user.id, fixo=True).count()
        for it in add[:30]:
            iid = str(it.get("item_id"))
            if not iid:
                continue
            reg = db.query(ShopeeBoostItem).filter_by(user_id=user.id, item_id=iid).first()
            if not reg:
                reg = ShopeeBoostItem(user_id=user.id, item_id=iid); db.add(reg)
            reg.nome = it.get("nome") or reg.nome
            if it.get("fixo") and fixos < 5:
                reg.fixo = True; fixos += 1
            if "prioridade" in it:
                reg.prioridade = int(it["prioridade"])
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.delete("/api/shopee/boost/itens/{item_id}")
def shopee_boost_remove(item_id: str, user: User = Depends(auth.get_current_user)):
    db = SessionLocal()
    try:
        from .models import ShopeeBoostItem
        db.query(ShopeeBoostItem).filter_by(user_id=user.id, item_id=item_id).delete()
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@app.post("/api/shopee/boost/itens/{item_id}/fixar")
def shopee_boost_fixar(item_id: str, payload: dict = Body(default={}),
                       user: User = Depends(auth.get_current_user)):
    db = SessionLocal()
    try:
        from .models import ShopeeBoostItem
        reg = db.query(ShopeeBoostItem).filter_by(user_id=user.id, item_id=item_id).first()
        if not reg:
            raise HTTPException(status_code=404, detail="Item não está na lista.")
        fixar = bool(payload.get("fixo", not reg.fixo))
        if fixar and db.query(ShopeeBoostItem).filter_by(user_id=user.id, fixo=True).count() >= 5:
            raise HTTPException(status_code=422, detail="Máximo de 5 produtos fixos.")
        reg.fixo = fixar
        db.commit()
        return {"ok": True, "fixo": reg.fixo}
    finally:
        db.close()


@app.post("/api/shopee/boost/reordenar")
def shopee_boost_reordenar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Reordena a fila: recebe a ordem desejada de item_ids (topo primeiro) e grava prioridades
    decrescentes. Ativa o critério 'prioridade' para a ordem manual valer no rodízio."""
    ordem = [str(x) for x in (payload.get("ordem") or []) if x]
    if not ordem:
        raise HTTPException(status_code=422, detail="Ordem vazia.")
    db = SessionLocal()
    try:
        from .models import ShopeeBoostItem, ShopeeBoostConfig
        n = len(ordem)
        for i, iid in enumerate(ordem):
            reg = db.query(ShopeeBoostItem).filter_by(user_id=user.id, item_id=iid).first()
            if reg:
                reg.prioridade = (n - i) * 10  # topo = maior prioridade
        cfg = db.query(ShopeeBoostConfig).filter_by(user_id=user.id).first()
        if not cfg:
            cfg = ShopeeBoostConfig(user_id=user.id); db.add(cfg)
        cfg.criterio = "prioridade"
        db.commit()
        return {"ok": True, "criterio": "prioridade"}
    finally:
        db.close()


@app.post("/api/shopee/boost/rodar")
def shopee_boost_rodar(user: User = Depends(auth.get_current_user)):
    """Roda um ciclo de boost agora (manual). O agendador faz isso sozinho."""
    try:
        return shopee_boost.ciclo(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/boost/sincronizar_nomes")
def shopee_boost_sincronizar_nomes(user: User = Depends(auth.get_current_user)):
    """Resolve os nomes reais dos produtos do boost (1 chamada Shopee, sob demanda).
    Separado do status para nunca travar o painel."""
    try:
        return shopee_boost.sincronizar_nomes(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/boost/desempenho")
def shopee_boost_desempenho(user: User = Depends(auth.get_current_user)):
    """Vendas dos últimos 30 dias por produto do boost — pra avaliar se o impulso está
    convertendo. Chama a Shopee (cacheada ~30min), por isso fica fora do status."""
    try:
        vendas = shopee.vendas_por_item(user.id, dias=30)  # {item_id(int): qtd}
    except Exception:  # noqa: BLE001
        vendas = {}
    return {"dias": 30, "vendas": {str(k): int(v) for k, v in (vendas or {}).items()}}


# ============================ CENTRAL DE BOOST (painel enterprise) ============================
_PICO_CACHE: dict = {}
BOOST_VAGA_HORAS = 4  # estimativa de duração p/ vaga em destaque sem boost_ate conhecido (reconciliação)
_LINHAS_CACHE: dict = {}
_ELEG_CACHE: dict = {}


def _boost_linhas_venda(user_id, dias=14, max_orders=500):
    """Linhas de venda (item_id, qtd, create_time_ms) dos últimos `dias`. Coleta os order_sn
    (barato) e detalha em lotes (para pegar create_time + itens). Cacheado 30min. Base
    compartilhada do heatmap e da atribuição de boost."""
    import time as _t
    ch = _LINHAS_CACHE.get(user_id)
    if ch and _t.time() - ch[0] < 1800:
        return ch[1]
    linhas = []
    try:
        sns, cursor, paginas = [], "", 0
        while paginas < 8 and len(sns) < max_orders:
            r = shopee.listar_pedidos(user_id, dias=dias, cursor=cursor, limite=100)
            resp = (r.get("response") or {}) if isinstance(r, dict) else {}
            for o in (resp.get("order_list") or []):
                if o.get("order_sn"):
                    sns.append(o["order_sn"])
            cursor = resp.get("next_cursor") or ""
            paginas += 1
            if not resp.get("more") or not cursor:
                break
        sns = sns[:max_orders]
        for i in range(0, len(sns), 50):
            rd = shopee.detalhe_pedidos(user_id, sns[i:i + 50])
            respd = (rd.get("response") or {}) if isinstance(rd, dict) else {}
            for o in (respd.get("order_list") or []):
                ct = o.get("create_time")
                if not ct:
                    continue
                ctms = int(ct) * 1000
                for it in (o.get("item_list") or []):
                    iid = it.get("item_id")
                    if iid:
                        qty = it.get("model_quantity_purchased") or it.get("quantity_purchased") or 0
                        linhas.append((str(iid), int(qty or 0), ctms))
    except Exception:  # noqa: BLE001
        pass
    _LINHAS_CACHE[user_id] = (_t.time(), linhas)
    return linhas


def _boost_vagas(user_id, st, vendas):
    """As 5 vagas ao vivo: lê o estado REAL da Shopee (get_boosted_list) e cruza com o nosso
    banco para rotular auto/manual/radar e o tempo restante. Preenche até 5 (livres no fim)."""
    from . import shopee_boost
    from .models import ShopeeBoostItem
    from datetime import datetime, timedelta
    agora = datetime.utcnow()
    reais = shopee_boost._boosted_ids(user_id)  # ids em destaque agora, ou None se não leu
    sincronizado = reais is not None
    if reais is None:
        reais = [str(x.get("item_id")) for x in (st.get("impulsionando") or [])]
    vagas, ordem = [], 0
    with SessionLocal() as db:
        regs = {str(i.item_id): i for i in db.query(ShopeeBoostItem).filter_by(user_id=user_id).all()}
        mudou = False
        for bid in reais[:5]:
            r = regs.get(str(bid))
            tipo, nome, impulsos, termina = "manual", "#" + str(bid), 0, None
            if r:
                # reconciliação leve: item em destaque agora sem boost_ate ganha estimativa de 4h
                # (some o "—"); só grava quando falta, então não fica reiniciando o relógio.
                if not (r.boost_ate and r.boost_ate > agora):
                    r.boost_ate = agora + timedelta(hours=BOOST_VAGA_HORAS)
                    mudou = True
                tipo = "radar" if r.condicional else ("auto" if r.auto else "manual")
                nome = r.nome if (r.nome and str(r.nome).lstrip("#") != str(r.item_id)) else ("#" + str(bid))
                impulsos = r.impulsos or 0
                termina = int((r.boost_ate - agora).total_seconds() * 1000)
            vagas.append({"ordem": ordem, "ocupada": True, "item_id": str(bid), "nome": nome,
                          "tipo": tipo, "termina_ms": termina, "impulsos": impulsos,
                          "vendas": int(vendas.get(str(bid), 0))})
            ordem += 1
        if mudou:
            try:
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
    while len(vagas) < 5:
        vagas.append({"ordem": ordem, "ocupada": False}); ordem += 1
    return vagas, sincronizado


def _abc_por_vendas(v):
    if v >= 30:
        return "A"
    if v >= 8:
        return "B"
    return "C"


def _boost_fila_enriquecida(fila, vendas, ocupadas):
    """Enriquece cada item da fila com giro (vendas/30d), Curva ABC e em quantos ciclos de 4h entra."""
    livres = max(0, 5 - ocupadas)
    out = []
    for idx, f in enumerate((fila or [])[:60]):
        iid = str(f.get("item_id"))
        v = int(vendas.get(iid, 0))
        ciclos = 0 if idx < livres else ((idx - livres) // 5 + 1)
        item = dict(f)
        item.update({"vendas": v, "giro": round(v / 30.0, 1),
                     "abc": _abc_por_vendas(v), "entra_ciclos": ciclos})
        out.append(item)
    return out


def _boost_campeoes(vendas, st):
    nomes = {}
    for x in (st.get("impulsionando") or []) + (st.get("fila") or []):
        nomes[str(x.get("item_id"))] = x.get("nome")
    top = sorted(vendas.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return [{"item_id": str(k), "nome": nomes.get(str(k)) or ("#" + str(k)), "vendas": int(v)}
            for k, v in top if v > 0]


def _boost_diario(user_id, limite=8):
    from .models import Notificacao
    try:
        with SessionLocal() as db:
            regs = (db.query(Notificacao)
                    .filter(Notificacao.user_id == user_id, Notificacao.modulo == "boost")
                    .order_by(Notificacao.id.desc()).limit(limite).all())
            return [{"tipo": "ok" if r.ok else "warn", "titulo": r.titulo, "texto": r.texto or "",
                     "quando": r.criado_em.isoformat() if r.criado_em else None} for r in regs]
    except Exception:  # noqa: BLE001
        return []


def _boost_painel(user_id):
    from . import shopee_boost, shopee_boost_auto
    from .models import ShopeeBoostItem
    from datetime import datetime
    st = shopee_boost.status(user_id)  # config + fila + impulsionando (só banco, rápido)
    try:
        vendas = {str(k): int(v) for k, v in (shopee.vendas_por_item(user_id, dias=30) or {}).items()}
    except Exception:  # noqa: BLE001
        vendas = {}
    vagas, sincronizado = _boost_vagas(user_id, st, vendas)
    ocupadas = sum(1 for v in vagas if v.get("ocupada"))
    agora = datetime.utcnow()
    with SessionLocal() as db:
        itens = db.query(ShopeeBoostItem).filter_by(user_id=user_id).all()
        impulsos_hoje = sum(1 for i in itens if i.ultimo_boost and i.ultimo_boost.date() == agora.date())
    rem = [v["termina_ms"] for v in vagas if v.get("ocupada") and v.get("termina_ms")]
    proxima = min(rem) if (rem and ocupadas >= 5) else None
    fila = _boost_fila_enriquecida(st.get("fila") or [], vendas, ocupadas)
    # info em lote (elegibilidade + imagem + sku + preço) para fila E vagas — 1 fetch cacheado
    ids_union = list({str(f.get("item_id")) for f in fila}
                     | {str(v.get("item_id")) for v in vagas if v.get("ocupada")})
    info = {}
    try:
        info = _boost_elegibilidade(user_id, ids_union)
    except Exception:  # noqa: BLE001
        info = {}
    precos = {}
    try:
        precos = _boost_precos(user_id, info)
    except Exception:  # noqa: BLE001
        precos = {}
    for f in fila:
        e = info.get(str(f.get("item_id"))) or {}
        f["elegivel"] = e.get("elegivel", True)
        f["motivo_ineleg"] = e.get("motivo")
        f["imagem"] = e.get("imagem")
        if e.get("estoque") is not None:
            f["estoque"] = e["estoque"]
        pr = precos.get(str(f.get("item_id")))
        if pr:
            f["preco"] = pr.get("preco")
            f["preco_sugerido"] = pr.get("sugerido")
            f["preco_ok"] = pr.get("ok")
            f["preco_status"] = pr.get("status")
    for v in vagas:
        if v.get("ocupada"):
            v["imagem"] = (info.get(str(v.get("item_id"))) or {}).get("imagem")
    nao_elegiveis = sum(1 for f in fila if f.get("elegivel") is False)
    preco_alerta = sum(1 for f in fila if f.get("preco_status") == "abaixo")
    # boost + promoção: marca quem já tem oferta ativa (exposição dobrada)
    try:
        ofertas = shopee.itens_em_campanha(user_id)
    except Exception:  # noqa: BLE001
        ofertas = set()
    def _oferta(iid):
        s = str(iid)
        return bool(s.isdigit() and int(s) in ofertas)
    for f in fila:
        f["em_oferta"] = _oferta(f.get("item_id"))
    for v in vagas:
        if v.get("ocupada"):
            v["em_oferta"] = _oferta(v.get("item_id"))
    em_oferta_fila = sum(1 for f in fila if f.get("em_oferta"))
    # sinais do condicional expandido (estoque/surto)
    try:
        sinais = _boost_sinais(user_id, fila)
    except Exception:  # noqa: BLE001
        sinais = []
    try:
        radar = {"config": shopee_boost_auto.config(user_id)}
        radar.update(shopee_boost_auto.avaliar(user_id))
    except Exception as e:  # noqa: BLE001
        radar = {"config": {}, "erro": str(e), "ameacados": []}
    radar["sinais"] = sinais
    return {
        "sincronizado": sincronizado,
        "config": {k: st.get(k) for k in ("ativo", "criterio", "janela_inicio", "janela_fim",
                                          "max_simultaneos", "auto_selecao", "auto_estrategia",
                                          "auto_maximo", "qtd_auto", "qtd_manual", "total", "fixos",
                                          "janelas", "cond_ativo", "cond_gatilho_pct", "cond_max",
                                          "cond_estoque", "cond_surto")},
        "vagas": vagas,
        "kpis": {"vagas_ocupadas": ocupadas, "vagas_total": 5, "na_fila": len(st.get("fila") or []),
                 "proxima_vaga_ms": proxima, "impulsos_hoje": impulsos_hoje, "impulsos_max_dia": 30,
                 "vendas_30d": sum(vendas.values()), "tem_vendas": bool(vendas),
                 "nao_elegiveis": nao_elegiveis, "em_oferta": em_oferta_fila, "sinais": len(sinais),
                 "preco_alerta": preco_alerta},
        "fila": fila,
        "campeoes": _boost_campeoes(vendas, st),
        "radar": radar,
        "diario": _boost_diario(user_id),
    }


def _boost_pico(user_id, dias=14):
    """Heatmap: pedidos por hora do dia (0–23), dos últimos `dias`. Usa os create_time reais
    das linhas de venda. Cacheado 1h. Sem dado => total 0 (estado honesto no painel)."""
    import time as _t
    from datetime import datetime, timezone, timedelta
    ch = _PICO_CACHE.get(user_id)
    if ch and _t.time() - ch[0] < 3600:
        return ch[1]
    horas = [0] * 24
    tz = timezone(timedelta(hours=-3))  # horário de Brasília
    vistos = set()
    for iid, qty, ctms in _boost_linhas_venda(user_id, dias=dias):
        chave = (ctms // 1000)  # 1 pedido pode ter várias linhas; conta a hora por evento de venda
        horas[datetime.fromtimestamp(ctms / 1000, tz).hour] += 1
    total = sum(horas)
    out = {"horas": horas, "total": total, "dias": dias}
    _PICO_CACHE[user_id] = (_t.time(), out)
    return out


def _boost_atribuir(user_id):
    """Atribui vendas às janelas de boost dos últimos 14 dias: soma as unidades vendidas do
    produto DENTRO de [inicio, fim] de cada boost. Grava em ShopeeBoostLog.vendas_atribuidas."""
    from .models import ShopeeBoostLog
    from datetime import datetime, timedelta, timezone
    linhas = _boost_linhas_venda(user_id)
    por_item = {}
    for iid, qty, ctms in linhas:
        por_item.setdefault(iid, []).append((ctms, qty))
    with SessionLocal() as db:
        limite = datetime.utcnow() - timedelta(days=14)
        logs = (db.query(ShopeeBoostLog)
                .filter(ShopeeBoostLog.user_id == user_id, ShopeeBoostLog.inicio >= limite).all())
        for lg in logs:
            vendas = por_item.get(str(lg.item_id))
            if vendas is None:
                continue  # sem cobertura de venda no período coletado
            ini = int(lg.inicio.replace(tzinfo=timezone.utc).timestamp() * 1000)
            fimdt = lg.fim or (lg.inicio + timedelta(hours=4))
            fim = int(fimdt.replace(tzinfo=timezone.utc).timestamp() * 1000)
            lg.vendas_atribuidas = sum(q for (ctms, q) in vendas if ini <= ctms <= fim)
            lg.atribuido_em = datetime.utcnow()
        db.commit()


def _boost_historico(user_id, limite=40):
    from .models import ShopeeBoostLog
    try:
        _boost_atribuir(user_id)
    except Exception:  # noqa: BLE001
        pass
    with SessionLocal() as db:
        logs = (db.query(ShopeeBoostLog).filter(ShopeeBoostLog.user_id == user_id)
                .order_by(ShopeeBoostLog.inicio.desc()).limit(limite).all())
        eventos = [{"item_id": l.item_id, "nome": l.nome or ("#" + l.item_id), "tipo": l.tipo,
                    "inicio": l.inicio.isoformat() if l.inicio else None,
                    "fim": l.fim.isoformat() if l.fim else None,
                    "vendas": l.vendas_atribuidas} for l in logs]
    resumo = {}
    for e in eventos:
        r = resumo.setdefault(e["item_id"], {"item_id": e["item_id"], "nome": e["nome"],
                                             "boosts": 0, "vendas": 0, "com_atrib": 0})
        r["boosts"] += 1
        if e["vendas"] is not None:
            r["vendas"] += e["vendas"]; r["com_atrib"] += 1
    lista = sorted(resumo.values(), key=lambda x: x["vendas"], reverse=True)
    for r in lista:
        r["por_boost"] = round(r["vendas"] / r["com_atrib"], 1) if r["com_atrib"] else None
    atrib = [e for e in eventos if e["vendas"] is not None]
    total_v = sum(e["vendas"] for e in atrib)
    # série diária: vendas totais vs. vendas durante destaque (últimos 14 dias, horário de Brasília)
    serie = []
    try:
        import time as _t
        from datetime import datetime, timezone, timedelta
        BR = timezone(timedelta(hours=-3))
        linhas = _boost_linhas_venda(user_id)
        with SessionLocal() as db:
            janelas = [(str(l.item_id),
                        l.inicio.replace(tzinfo=timezone.utc).timestamp() * 1000,
                        (l.fim.replace(tzinfo=timezone.utc).timestamp() * 1000) if l.fim else _t.time() * 1000)
                       for l in db.query(ShopeeBoostLog).filter(ShopeeBoostLog.user_id == user_id).all() if l.inicio]
        agora = _t.time() * 1000
        dias = {}
        for d in range(13, -1, -1):
            k = datetime.fromtimestamp((agora - d * 86400000) / 1000, tz=BR).strftime("%d/%m")
            dias[k] = {"d": k, "total": 0, "boost": 0}
        for iid, qty, ctms in linhas:
            k = datetime.fromtimestamp(ctms / 1000, tz=BR).strftime("%d/%m")
            if k not in dias:
                continue
            dias[k]["total"] += qty
            if any(str(iid) == jid and a <= ctms <= b for jid, a, b in janelas):
                dias[k]["boost"] += qty
        serie = list(dias.values())
    except Exception:  # noqa: BLE001
        serie = []
    return {"eventos": eventos, "resumo": lista, "serie": serie,
            "kpis": {"total_boosts": len(eventos), "total_vendas_atrib": total_v,
                     "vendas_por_boost": round(total_v / len(atrib), 1) if atrib else None,
                     "boosts_atribuidos": len(atrib), "produtos": len(lista)}}


def _boost_elegibilidade(user_id, item_ids):
    """Elegibilidade real de cada anúncio para boost: precisa estar ativo (NORMAL) e com estoque.
    Usa get_item_base_info em lote (≤50), cacheado 10min. Sem info => assume elegível."""
    import time as _t
    ids = [str(i) for i in item_ids if i][:100]
    ch = _ELEG_CACHE.get(user_id)
    if ch and _t.time() - ch[0] < 600:
        base = ch[1]
    else:
        base = {}
        try:
            for i in range(0, len(ids), 50):
                r = shopee.info_itens(user_id, [int(x) for x in ids[i:i + 50]])
                for x in ((r.get("response") or {}).get("item_list") or []):
                    imgs = (x.get("image") or {}).get("image_url_list") or []
                    precos = x.get("price_info") or []
                    base[str(x.get("item_id"))] = {
                        "status": x.get("item_status"),
                        "estoque": (x.get("stock_info_v2") or {}).get("summary_info", {}).get("total_available_stock"),
                        "imagem": imgs[0] if imgs else None,
                        "sku": x.get("item_sku"),
                        "preco": (precos[0].get("current_price") if precos else None),
                    }
        except Exception:  # noqa: BLE001
            base = {}
        _ELEG_CACHE[user_id] = (_t.time(), base)
    _MOTIVO = {"BANNED": "banido", "DELETED": "excluído", "UNLIST": "inativo",
               "REVIEWING": "em revisão", "SELLER_DELETE": "excluído", "SHOPEE_DELETE": "removido"}
    out = {}
    for iid in ids:
        info = base.get(iid)
        if not info:
            out[iid] = {"elegivel": True, "motivo": None, "estoque": None, "imagem": None, "sku": None, "preco": None}
            continue
        st = (info.get("status") or "").upper()
        est = info.get("estoque")
        extra = {"estoque": est, "imagem": info.get("imagem"), "sku": info.get("sku"), "preco": info.get("preco")}
        if st and st != "NORMAL":
            out[iid] = {"elegivel": False, "motivo": _MOTIVO.get(st, st.lower()), **extra}
        elif est is not None and est <= 0:
            out[iid] = {"elegivel": False, "motivo": "sem estoque", **extra}
        else:
            out[iid] = {"elegivel": True, "motivo": None, **extra}
    return out


def _boost_sinais(user_id, fila):
    """Sinais do boost condicional expandido, dos dados reais: 'prestes a esgotar' (estoque baixo
    com giro) e 'surto de vendas' (aceleração dos últimos 3d vs. a base de 11d)."""
    import time as _t
    sinais = []
    linhas = _boost_linhas_venda(user_id)
    corte = _t.time() * 1000 - 3 * 86400000
    rec, base = {}, {}
    for iid, qty, ctms in linhas:
        alvo = rec if ctms >= corte else base
        alvo[iid] = alvo.get(iid, 0) + qty
    for f in fila:
        if f.get("elegivel") is False:
            continue
        iid = str(f.get("item_id"))
        est = f.get("estoque")
        giro = f.get("giro") or 0
        if est is not None and giro > 0 and 0 < est <= giro * 3:
            sinais.append({"item_id": iid, "nome": f.get("nome"), "tipo": "estoque",
                           "detalhe": f"{est} un · ~{round(est / giro, 1)}d de estoque"})
            continue
        r3, b11 = rec.get(iid, 0) / 3.0, base.get(iid, 0) / 11.0
        if r3 >= 1 and b11 > 0 and r3 >= 1.6 * b11:
            sinais.append({"item_id": iid, "nome": f.get("nome"), "tipo": "surto",
                           "detalhe": f"vendas {round(r3 / b11, 1)}x acima do normal"})
    return sinais[:8]


def _boost_precos(user_id, info):
    """Cruza o preço ATUAL de cada item na Shopee com o preço-sugerido da régua (modelo
    BASE-VENDA): o preço Bling é o líquido-alvo e a régua dá o preço de LISTA na Shopee que o
    preserva. ok = preço atual bate com o sugerido; senão 'abaixo' (furando a margem) ou 'acima'."""
    try:
        from . import catalogo, precificacao
        cfg_prec = precificacao.obter_config(user_id)
        cat = {p["sku"]: p for p in catalogo.todos(user_id) if p.get("sku")}
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for iid, d in (info or {}).items():
        sku, preco = d.get("sku"), d.get("preco")
        base = cat.get(sku) if sku else None
        if not base or preco is None:
            continue
        bling = float(base.get("preco") or 0)  # líquido-alvo (preço Bling)
        if bling <= 0:
            continue
        try:
            sug = precificacao.avaliar_com_cfg(cfg_prec, 0, bling, "shopee").get("preco_sugerido")
        except Exception:  # noqa: BLE001
            sug = None
        if not sug:
            continue
        preco = float(preco)
        status = "abaixo" if preco < sug * 0.99 else ("acima" if preco > sug * 1.08 else "ok")
        out[iid] = {"ok": status == "ok", "status": status, "sugerido": round(float(sug), 2),
                    "preco": round(preco, 2), "bling": round(bling, 2)}
    return out


def _boost_pico_old(user_id, dias=30):
    """Heatmap de vendas por hora do dia (0–23), dos últimos `dias`. Usa o create_time dos
    pedidos (barato: só a lista, sem detalhe). Cacheado 1h. Sem dado suficiente => total 0."""
    import time as _t
    from datetime import datetime, timezone, timedelta
    ch = _PICO_CACHE.get(user_id)
    if ch and _t.time() - ch[0] < 3600:
        return ch[1]
    horas = [0] * 24
    total = 0
    tz = timezone(timedelta(hours=-3))  # horário de Brasília
    try:
        cursor, paginas = "", 0
        while paginas < 10:
            r = shopee.listar_pedidos(user_id, dias=dias, cursor=cursor, limite=100)
            resp = (r.get("response") or {}) if isinstance(r, dict) else {}
            for o in (resp.get("order_list") or []):
                ct = o.get("create_time")
                if ct:
                    horas[datetime.fromtimestamp(int(ct), tz).hour] += 1
                    total += 1
            cursor = resp.get("next_cursor") or ""
            paginas += 1
            if not resp.get("more") or not cursor:
                break
    except Exception:  # noqa: BLE001
        pass
    out = {"horas": horas, "total": total, "dias": dias}
    _PICO_CACHE[user_id] = (_t.time(), out)
    return out


@app.get("/api/shopee/boost/painel")
def shopee_boost_painel(user: User = Depends(auth.get_current_user)):
    """Painel completo do impulsionamento: vagas ao vivo, KPIs, fila enriquecida, campeões,
    Radar e diário — tudo em dado real (rápido; o heatmap fica em /boost/pico)."""
    try:
        return _boost_painel(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/boost/pico")
def shopee_boost_pico(user: User = Depends(auth.get_current_user)):
    """Heatmap: vendas por hora do dia (últimos 30 dias). Cacheado 1h."""
    return _boost_pico(user.id)


@app.get("/api/shopee/boost/historico")
def shopee_boost_historico(user: User = Depends(auth.get_current_user)):
    """Histórico durável de boosts + atribuição de vendas por janela (lift real por produto)."""
    return _boost_historico(user.id)


# ---- Avaliações ----
@app.get("/api/shopee/avaliacoes")
def shopee_avaliacoes(status: str = "UNANSWERED", cursor: str = "", item_id: str = "",
                      user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_avaliacoes(user.id, item_id=item_id or None, status=status, cursor=cursor)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/reputacao/painel")
def shopee_reputacao_painel(forcar: int = 0, user: User = Depends(auth.get_current_user)):
    """Central de Reputação: KPIs, distribuição, tendência, radar de compradores,
    saúde da conta, config e atividade do copiloto — em uma chamada."""
    from . import shopee_reputacao
    try:
        return shopee_reputacao.painel(user.id, forcar=bool(forcar))
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/reputacao/temas")
def shopee_reputacao_temas(forcar: int = 0, user: User = Depends(auth.get_current_user)):
    """Temas dos comentários lidos por IA (positivo/negativo + citações). Cacheado ~6h."""
    from . import shopee_reputacao
    try:
        return shopee_reputacao.temas_ia(user.id, forcar=bool(forcar))
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/reputacao/comprador/{usuario}")
def shopee_reputacao_comprador(usuario: str, user: User = Depends(auth.get_current_user)):
    """Dossiê de um comprador: pedidos, avaliações deixadas e linha do tempo."""
    from . import shopee_reputacao
    try:
        return shopee_reputacao.dossie(user.id, usuario)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/reputacao/temas")
def shopee_reputacao_temas(forcar: int = 0, user: User = Depends(auth.get_current_user)):
    """Análise de temas/sentimento dos comentários pela IA (cacheado 1h)."""
    from . import shopee_reputacao
    return shopee_reputacao.temas(user.id, forcar=bool(forcar))


@app.post("/api/shopee/reputacao/responder_massa")
def shopee_reputacao_responder_massa(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Gera (IA) e envia respostas para uma lista de avaliações de uma vez. Body:
    {itens:[{comment_id, nota, comentario, produto?, nome?}]}. Retorna por item o resultado."""
    itens = payload.get("itens") or []
    if not itens:
        raise HTTPException(status_code=422, detail="Nenhuma avaliação selecionada.")
    resultados = []
    for it in itens[:100]:
        cid = it.get("comment_id")
        try:
            texto = it.get("texto") or shopee_reviews.sugerir(
                user.id, it.get("nota", 5), it.get("comentario", ""),
                it.get("produto"), it.get("nome"))
            shopee.responder_avaliacao(user.id, cid, texto)
            try:
                shopee_reviews._registrar_log(user.id, cid, it.get("nota", 5),
                                              it.get("nome"), it.get("produto"), texto, modo="massa")
            except Exception:  # noqa: BLE001
                pass
            resultados.append({"comment_id": cid, "ok": True})
        except Exception as e:  # noqa: BLE001
            resultados.append({"comment_id": cid, "ok": False, "erro": str(e)})
    ok = sum(1 for r in resultados if r["ok"])
    return {"enviadas": ok, "falhas": len(resultados) - ok, "resultados": resultados}


@app.post("/api/shopee/avaliacoes/responder")
def shopee_avaliacoes_responder(payload: dict = Body(...),
                                user: User = Depends(auth.get_current_user)):
    """Responde uma avaliação. Body: {comment_id, texto, ia?, nota?, comentario?}.
    Se ia=true, gera a resposta com IA a partir da nota e do comentário do cliente."""
    cid = payload.get("comment_id")
    texto = payload.get("texto")
    if payload.get("ia") and not texto:
        nota = payload.get("nota", 5)
        coment = payload.get("comentario", "")
        prompt = (
            "Você é o dono de uma loja de armarinho na Shopee respondendo a uma avaliação. "
            "Seja cordial, breve, humano e em português do Brasil. "
            f"Nota: {nota}/5. Comentário do cliente: \"{coment}\". "
            "Se a nota for alta, agradeça com calor; se for baixa, peça desculpas, mostre que se "
            "importa e ofereça resolver pelo chat da Shopee. Responda SÓ com o texto da resposta."
        )
        try:
            texto = ai._gerar_texto(user.id, prompt)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"IA falhou: {e}")
    if not cid or not texto:
        raise HTTPException(status_code=422, detail="Informe comment_id e texto (ou ia=true).")
    try:
        shopee.responder_avaliacao(user.id, cid, texto)
        return {"ok": True, "texto": texto}
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/avaliacoes/config")
def shopee_review_config_get(user: User = Depends(auth.get_current_user)):
    return shopee_reviews.obter_config(user.id)


@app.put("/api/shopee/avaliacoes/config")
def shopee_review_config_put(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    return shopee_reviews.salvar_config(user.id, payload)


@app.get("/api/shopee/impressao/config")
def shopee_impressao_config_get(user: User = Depends(auth.get_current_user)):
    """Config de impressão da conta: dados do emitente + campos visíveis na folha/etiqueta."""
    return shopee_impressao.obter_config(user.id)


@app.put("/api/shopee/impressao/config")
def shopee_impressao_config_put(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    return shopee_impressao.salvar_config(user.id, payload)


@app.post("/api/shopee/avaliacoes/sugerir")
def shopee_review_sugerir(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Gera um rascunho de resposta no padrão da loja — SEM enviar. Para o modo manual.
    Body: {nota, comentario, produto?, nome?, tom?}."""
    try:
        texto = shopee_reviews.sugerir(
            user.id, payload.get("nota", 5), payload.get("comentario", ""),
            payload.get("produto"), payload.get("nome"), payload.get("tom"),
            vip=bool(payload.get("vip", False)))
        return {"texto": texto}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"IA falhou: {e}")


@app.post("/api/shopee/avaliacoes/auto_responder")
def shopee_review_auto(background_tasks: BackgroundTasks, user: User = Depends(auth.get_current_user)):
    """Dispara o modo automático agora, em SEGUNDO PLANO: responde as avaliações sem
    resposta cujas notas estão em auto_estrelas, com pausa entre cada uma (anti-flood na
    API da Shopee). Roda fora da requisição para não estourar o timeout do gateway."""
    background_tasks.add_task(shopee_reviews.auto_responder, user.id)
    return {"acao": "iniciado",
            "mensagem": "O agente começou a responder em segundo plano, com pausa entre as respostas. "
                        "Atualize a lista em instantes para ver as respostas aparecendo."}


@app.post("/api/shopee/avaliacoes/mutirao")
def shopee_review_mutirao(payload: dict = Body(default={}), user: User = Depends(auth.get_current_user)):
    """Responde a fila INTEIRA de pendentes (notas-alvo), em segundo plano, com pausa
    entre cada resposta e progresso ao vivo (acompanhe em /atividade).
    Body opcional: {completo: true} varre TODOS os produtos para alcançar avaliações antigas."""
    return shopee_reviews.iniciar_mutirao(user.id, completo=bool((payload or {}).get("completo")))


@app.post("/api/shopee/avaliacoes/parar")
def shopee_review_parar(user: User = Depends(auth.get_current_user)):
    """Interrompe o mutirão do agente no próximo item."""
    return shopee_reviews.parar_agente(user.id)


@app.get("/api/shopee/avaliacoes/atividade")
def shopee_review_atividade(user: User = Depends(auth.get_current_user)):
    """Estado vivo do agente: progresso do lote atual, feed das últimas respostas e contagem."""
    return shopee_reviews.atividade(user.id)


@app.post("/api/shopee/avaliacoes/contar")
def shopee_review_contar(background_tasks: BackgroundTasks, forcar: int = 1,
                         user: User = Depends(auth.get_current_user)):
    """Dispara a contagem (respondidas x pendentes) em segundo plano — pagina a Shopee.
    forcar=0 usa o cache (~10min) se houver; forcar=1 recalcula. Resultado em /atividade."""
    background_tasks.add_task(shopee_reviews.contar_avaliacoes, user.id, 60, bool(forcar))
    return {"acao": "contando", "mensagem": "Contando suas avaliações em segundo plano…"}


# ----------------------- Central de Promoções (painel MAX) ----------------- #
@app.get("/api/shopee/promo/painel")
def shopee_promo_painel_get(forcar: bool = False, user: User = Depends(auth.get_current_user)):
    try:
        return shopee_promo_painel.painel(user.id, forcar=forcar)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Falha ao montar painel: {e}")


@app.get("/api/shopee/promo/trava")
def shopee_promo_trava(user: User = Depends(auth.get_current_user)):
    """IDs dos itens que JÁ estão em campanha de desconto (ativa/agendada) — a trava
    anti-duplicação. O seletor manual usa para desabilitar esses produtos."""
    try:
        ids = sorted(int(i) for i in shopee.itens_em_campanha(user.id))
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ids": ids, "total": len(ids)}


# ----------------------- Motor de promoções automáticas ------------------- #
@app.get("/api/shopee/promo/config")
def shopee_promo_config_get(user: User = Depends(auth.get_current_user)):
    return shopee_promo_auto.obter_config(user.id)


@app.put("/api/shopee/promo/config")
def shopee_promo_config_put(background_tasks: BackgroundTasks, payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    antes = shopee_promo_auto.obter_config(user.id)
    cfg = shopee_promo_auto.salvar_config(user.id, payload)
    # Acabou de ligar o modo automático? roda um ciclo na hora (em segundo plano),
    # pra "selecionar auto" já produzir ação — depois o agendador mantém a cadência.
    virou_auto = cfg.get("ativo") and cfg.get("modo") == "auto" and (
        not antes.get("ativo") or antes.get("modo") != "auto")
    if virou_auto:
        background_tasks.add_task(shopee_promo_auto.auto_ciclo_forcado, user.id)
    return {**cfg, "disparo_imediato": bool(virou_auto)}


@app.post("/api/shopee/promo/diagnosticar")
def shopee_promo_diagnosticar(user: User = Depends(auth.get_current_user)):
    """Testa anexar 1 produto a um desconto e devolve as respostas CRUAS da Shopee,
    pra revelar o motivo exato do '0 produtos'. Apaga o desconto de teste no fim."""
    try:
        return shopee_promo_auto.diagnosticar_desconto(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/promo/diagnosticar-flash")
def shopee_promo_diagnosticar_flash(user: User = Depends(auth.get_current_user)):
    """Testa criar 1 oferta relâmpago (Flash Sale da loja) e devolve as respostas CRUAS da
    Shopee, revelando se a loja tem slots/elegibilidade. Apaga a oferta de teste no fim."""
    try:
        return shopee_promo_auto.diagnosticar_flash(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/promo/propor")
def shopee_promo_propor(user: User = Depends(auth.get_current_user)):
    """Monta as propostas de promoção (não cria nada) — o agente escolhe os produtos
    e calcula o desconto seguro dentro das regras."""
    try:
        return shopee_promo_auto.propor(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/promo/rodar")
def shopee_promo_rodar(background_tasks: BackgroundTasks, user: User = Depends(auth.get_current_user)):
    """Roda o agente AGORA e APLICA as promoções (sem aprovação), em segundo plano.
    Para o modo automático: o usuário clica e o agente aplica sozinho, dentro das travas."""
    background_tasks.add_task(shopee_promo_auto.aplicar_agora, user.id)
    return {"acao": "iniciado",
            "mensagem": "O agente está montando e aplicando as promoções em segundo plano. "
                        "Confira o histórico em instantes."}


@app.post("/api/shopee/promo/aplicar")
def shopee_promo_aplicar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Cria as promoções a partir das propostas aprovadas. Body: {propostas, tipo?}."""
    propostas = payload.get("propostas") or []
    if not propostas:
        raise HTTPException(status_code=422, detail="Envie as propostas a aplicar.")
    try:
        return shopee_promo_auto.aplicar(user.id, propostas, payload.get("tipo"), "manual")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/promo/queda")
def shopee_promo_queda(user: User = Depends(auth.get_current_user)):
    """Estado da detecção de queda de vendas (linha de base x atual)."""
    return shopee_promo_auto.detectar_queda(user.id)


@app.get("/api/shopee/promo/historico")
def shopee_promo_historico(user: User = Depends(auth.get_current_user)):
    return {"itens": shopee_promo_auto.historico(user.id, limite=40),
            "resumo": shopee_promo_auto.resumo(user.id)}


@app.get("/api/shopee/pedidos/inteligencia")
def shopee_pedidos_inteligencia(dias: int = 45, user: User = Depends(auth.get_current_user)):
    """Ponte Pedidos → Campanhas: sugestões de Leve+/Add-on/Cupom a partir dos pedidos reais."""
    try:
        return shopee_promo_agentes.inteligencia_vendas(user.id, dias=min(max(dias, 7), 90))
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/promo/diag")
def shopee_promo_diag(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """DIAGNÓSTICO TEMPORÁRIO multi-tipo (desconto|bundle|addon). Cria uma promoção de teste,
    adiciona itens reais, captura a RESPOSTA CRUA da Shopee ao adicionar (o que precisamos ver),
    reconsulta para conferir se entrou de fato, e tenta encerrar/apagar o teste. Não altera o wrapper."""
    import time as _t
    passos = []
    def log(passo, dado):
        passos.append({"passo": passo, "dado": dado})
    def chamar(path, extra, metodo="POST", rot=None):
        try:
            r = shopee._chamar(user.id, path, metodo=metodo, extra=extra)
            if rot:
                log(rot, r)
            return r
        except Exception as e:
            if rot:
                log(rot + "_ERRO", str(e))
            return {"_erro": str(e)}

    tipo = (payload.get("tipo") or "desconto").lower()
    dpct = int(payload.get("desconto_pct", 10))
    try:
        item_id = int(payload["item_id"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Informe um item_id da Shopee.")

    # dados reais do item selecionado (+ um segundo item da loja, p/ bundle/add-on)
    def preco_item(iid):
        try:
            nm = (shopee.nomes_itens(user.id, [iid]) or {}).get(iid, {}) or {}
            return float(nm.get("preco") or 0), nm.get("nome")
        except Exception:
            return 0.0, None
    p1, nome1 = preco_item(item_id)
    log("item_selecionado", {"item_id": item_id, "nome": nome1, "preco": p1})
    item_id2 = None
    if tipo in ("bundle", "addon"):
        try:
            r = shopee.listar_itens(user.id, offset=0, limite=8)
            outros = [int(x["item_id"]) for x in (r.get("response") or {}).get("item") or []
                      if int(x.get("item_id") or 0) != item_id]
            item_id2 = outros[0] if outros else None
        except Exception as e:
            log("erro_buscar_segundo_item", str(e))
        p2, nome2 = preco_item(item_id2) if item_id2 else (0.0, None)
        log("segundo_item", {"item_id": item_id2, "nome": nome2, "preco": p2})

    ini = int(_t.time()) + 3900
    fim = ini + 3 * 86400

    if tipo == "desconto":
        item_list = shopee.itens_desconto_por_pct(user.id, [{"item_id": item_id, "desconto_pct": dpct, "preco": p1}])
        log("item_list_ENVIADO", item_list)
        r = chamar("/api/v2/discount/add_discount", {"discount_name": f"DIAG {int(_t.time())}", "start_time": ini, "end_time": fim}, rot="resp_add_discount")
        did = (r.get("response") or {}).get("discount_id")
        if did and item_list:
            chamar("/api/v2/discount/add_discount_item", {"discount_id": did, "item_list": item_list}, rot="resp_add_discount_item_CRUA")
            rg = chamar("/api/v2/discount/get_discount", {"discount_id": did, "page_no": 1, "page_size": 50}, metodo="GET")
            it = (rg.get("response") or {}).get("item_list") or []
            log("get_discount_item_count", len(it))
            chamar("/api/v2/discount/delete_discount", {"discount_id": did}, rot="limpeza_delete")

    elif tipo == "bundle":
        ids = [i for i in [item_id, item_id2] if i]
        r = chamar("/api/v2/bundle_deal/add_bundle_deal", {"name": f"DIAG {int(_t.time())}", "start_time": ini, "end_time": fim,
                   "bundle_deal_rule": {"rule_type": 2, "discount_value": 10, "min_amount": 2, "max_amount": 0}}, rot="resp_add_bundle")
        bid = (r.get("response") or {}).get("bundle_deal_id")
        log("bundle_deal_id", bid)
        if bid and ids:
            # variação A: item_list só com item_id
            chamar("/api/v2/bundle_deal/add_bundle_deal_item", {"bundle_deal_id": bid,
                   "item_list": [{"item_id": i} for i in ids]}, rot="resp_add_bundle_item_A_soItemId")
            # variação B: item_list com status:1 (para comparar)
            chamar("/api/v2/bundle_deal/add_bundle_deal_item", {"bundle_deal_id": bid,
                   "item_list": [{"item_id": i, "status": 1} for i in ids]}, rot="resp_add_bundle_item_B_comStatus")
            rg = chamar("/api/v2/bundle_deal/get_bundle_deal_item", {"bundle_deal_id": bid}, metodo="GET", rot="get_bundle_item")
            chamar("/api/v2/bundle_deal/delete_bundle_deal", {"bundle_deal_id": bid}, rot="limpeza_delete")

    elif tipo == "addon":
        r = chamar("/api/v2/add_on_deal/add_add_on_deal", {"add_on_deal_name": f"DIAG {int(_t.time())}",
                   "start_time": ini, "end_time": fim, "promotion_type": 0}, rot="resp_add_addon")
        aid = (r.get("response") or {}).get("add_on_deal_id")
        log("add_on_deal_id", aid)
        if aid:
            chamar("/api/v2/add_on_deal/add_add_on_deal_main_item", {"add_on_deal_id": aid,
                   "main_item_list": [{"item_id": item_id, "status": 1}]}, rot="resp_add_addon_MAIN_CRUA")
            if item_id2:
                p2, _ = preco_item(item_id2)
                sub_price = round(max(0.5, (p2 or 10) * 0.8), 2)
                log("sub_add_on_deal_price_ENVIADO", {"item_id": item_id2, "preco_base": p2, "add_on_deal_price": sub_price})
                chamar("/api/v2/add_on_deal/add_add_on_deal_sub_item", {"add_on_deal_id": aid,
                       "sub_item_list": [{"item_id": item_id2, "add_on_deal_price": sub_price, "status": 1}]}, rot="resp_add_addon_SUB_CRUA")
            chamar("/api/v2/add_on_deal/get_add_on_deal", {"add_on_deal_id": aid}, metodo="GET", rot="get_addon")
            chamar("/api/v2/add_on_deal/delete_add_on_deal", {"add_on_deal_id": aid}, rot="limpeza_delete")
    else:
        raise HTTPException(status_code=422, detail="tipo inválido (use desconto, bundle ou addon).")

    return {"tipo": tipo, "passos": passos}


@app.get("/api/admin/logs")
def admin_logs(n: int = 200, nivel: str | None = None,
               user: User = Depends(auth.get_current_user)):
    """Últimos registros de log em memória (espelho do que vai para o Railway)."""
    return {"logs": observ.logs_recentes(n=min(n, 800), nivel=nivel),
            "total_buffer": len(observ._BUFFER)}


# ---- Pedidos & financeiro ----
@app.get("/api/shopee/pedidos")
def shopee_pedidos(dias: int = 7, cursor: str = "", user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_pedidos(user.id, dias=dias, cursor=cursor)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/pedidos/{order_sn}/detalhe")
def shopee_pedido_detalhe(order_sn: str, user: User = Depends(auth.get_current_user)):
    """Detalhe completo de um pedido (produtos com margem real + repasse + comprador + logística)."""
    try:
        return shopee.pedido_detalhe(user.id, order_sn)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/pedidos/painel")
def shopee_pedidos_painel(status: str = "A_ENVIAR", dias: int = 15, page: int = 1, page_size: int = 20,
                          busca: str = "", busca_tipo: str = "tudo", grupo: str = "todos", nf: str = "todos",
                          user: User = Depends(auth.get_current_user)):
    """Pedidos enriquecidos + análise de valor (pago x preço de tabela), PAGINADO.
    Filtros: status (Meus Pedidos), grupo (aberto/concluído), nf (situação da NF), busca+busca_tipo."""
    try:
        r = shopee.pedidos_painel(user.id, status=status, dias=dias, page=page, page_size=page_size,
                                  busca=busca, busca_tipo=busca_tipo, grupo=grupo, nf=nf)
        # A Shopee nem sempre manda `image_info` no pedido — o catálogo (que já temos) tem a
        # foto por SKU. Preenche o que faltar para o card nunca ficar sem imagem.
        try:
            faltam = any((not i.get("imagem")) for p in (r.get("pedidos") or []) for i in (p.get("itens") or []))
            if faltam:
                from . import catalogo as _cat
                fotos = {}
                for prod in (_cat.todos(user.id) or []):
                    sku = prod.get("sku")
                    img = prod.get("imagem") or prod.get("foto") or prod.get("image")
                    if sku and img:
                        fotos[str(sku)] = img
                if fotos:
                    n = 0
                    for p in (r.get("pedidos") or []):
                        for i in (p.get("itens") or []):
                            if not i.get("imagem"):
                                img = fotos.get(str(i.get("sku") or ""))
                                if img:
                                    i["imagem"] = img
                                    n += 1
                    if n:
                        print(f"[shopee] {n} imagem(ns) preenchidas pelo catálogo", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[shopee] fallback de imagem falhou: {e}", flush=True)
        return r
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/pedidos/contagens")
def shopee_pedidos_contagens(dias: int = 15, user: User = Depends(auth.get_current_user)):
    """Contadores por status (selos das abas) — chamada barata, só lista de SNs."""
    try:
        return shopee.contagens_status(user.id, dias=dias)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/pedidos/contagens-nf")
def shopee_pedidos_contagens_nf(status: str = "TODOS", dias: int = 15, user: User = Depends(auth.get_current_user)):
    """Contadores por situação de NF (Bling) + selos por pedido. Chamado de forma assíncrona."""
    try:
        return shopee.contagens_nf(user.id, status=status, dias=dias)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/pedidos/separacao")
def shopee_pedidos_separacao(status: str = "A_ENVIAR", dias: int = 15, user: User = Depends(auth.get_current_user)):
    """Lista de separação (produtos em ordem alfabética + quantidade total)."""
    try:
        return shopee.lista_separacao(user.id, status=status, dias=dias)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/pedidos/enriquecer-impressao")
def shopee_pedidos_enriquecer_impressao(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Enriquece os pedidos selecionados para impressão (etiqueta/folha): rastreio +
    NF-e (casada por numeroPedidoLoja) + descrição complementar por SKU.
    Body: {order_sns:[...], skus:[...]}. Retorna {patches:{order_sn:{...}}, complementos:{sku:texto}}."""
    try:
        return shopee.enriquecer_impressao(user.id,
                                           payload.get("order_sns") or [],
                                           payload.get("skus") or [])
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/pedidos/etiqueta-oficial")
def shopee_etiqueta_oficial(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Gera e baixa o waybill OFICIAL da Shopee (PDF) para os pedidos selecionados.
    Fluxo: create_shipping_document -> get_result (poll) -> download. Conta de vendedor
    precisa estar validada. Body: {order_sns:[...], tipo?:'auto'}. Retorna o PDF (application/pdf)."""
    order_sns = payload.get("order_sns") or []
    if not order_sns:
        raise HTTPException(status_code=400, detail="Informe ao menos um pedido.")
    try:
        pdf = shopee.gerar_etiqueta_oficial(user.id, order_sns, payload.get("tipo") or "auto")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    nome = "etiqueta-shopee.pdf" if len(order_sns) == 1 else f"etiquetas-shopee-{len(order_sns)}.pdf"
    return Response(content=pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{nome}"'})


@app.get("/api/shopee/financeiro/margem-real")
def shopee_margem_real(dias: int = 7, limite: int = 40, user: User = Depends(auth.get_current_user)):
    """Margem líquida REAL por pedido: repasse da Shopee × custo do produto. Caro — cacheado ~20min."""
    try:
        return shopee.margem_real(user.id, dias=dias, limite_pedidos=limite)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/pedidos/{order_sn}/repasse")
def shopee_repasse(order_sn: str, user: User = Depends(auth.get_current_user)):
    """Escrow: valor líquido real recebido (preço − comissão − taxas)."""
    try:
        return shopee.repasse_pedido(user.id, order_sn)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Promoções: descontos ----
@app.get("/api/shopee/descontos")
def shopee_descontos(status: str = "ongoing", user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_descontos(user.id, status=status)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/descontos")
def shopee_criar_desconto(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Body: {nome, inicio (epoch), fim (epoch), itens:[{item_id, promotion_price, ...}]}."""
    try:
        itens = payload.get("itens", [])
        res = shopee.criar_desconto(user.id, payload["nome"], int(payload["inicio"]),
                                    int(payload["fim"]), itens)
        if itens and not res.get("itens_adicionados"):
            motivo = (res.get("item_erros") or ["nenhum produto elegível entrou"])[0]
            raise HTTPException(status_code=502,
                detail=f"O desconto foi criado, mas SEM produtos: {motivo}. "
                       "Verifique se os anúncios estão ativos e elegíveis a desconto.")
        return res
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Informe nome, inicio, fim e itens.")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/campanhas/agenda")
def shopee_campanhas_agenda(user: User = Depends(auth.get_current_user)):
    """Todas as campanhas (todos os tipos) normalizadas pra visão geral / timeline."""
    try:
        return shopee.agenda_campanhas(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/campanhas/dashboard")
def shopee_campanhas_dashboard(dias: int = 30, user: User = Depends(auth.get_current_user)):
    """Receita gerada por promoções no período (uma varredura, atribuída por promotion_id). Cacheado."""
    try:
        return shopee.dashboard_promo(user.id, dias=dias)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/campanha/{tipo}/{cid}/desempenho")
def shopee_campanha_desempenho(tipo: str, cid: str, user: User = Depends(auth.get_current_user)):
    """Vendas dos produtos da campanha no período dela (pedidos × produtos). Caro — cacheado ~10min."""
    try:
        return shopee.desempenho_campanha(user.id, tipo, cid)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/campanha/{tipo}/{cid}/repetir")
def shopee_campanha_repetir(tipo: str, cid: str, user: User = Depends(auth.get_current_user)):
    """Recria a campanha igual, com novo período (mesma duração, começando em ~5 min)."""
    try:
        return shopee.repetir_campanha(user.id, tipo, cid)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/campanha/{tipo}/{cid}")
def shopee_campanha_detalhe(tipo: str, cid: str, user: User = Depends(auth.get_current_user)):
    """Detalhe de uma campanha COM os produtos (nome, imagem, preço de/por).
    tipo: desconto | bundle | addon | flash."""
    fn = {"desconto": shopee.detalhe_desconto, "bundle": shopee.detalhe_bundle,
          "addon": shopee.detalhe_addon, "flash": shopee.detalhe_flash}.get(tipo)
    if not fn:
        raise HTTPException(status_code=404, detail="Tipo de campanha desconhecido.")
    try:
        return fn(user.id, cid)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))
    try:
        return shopee.encerrar_desconto(user.id, discount_id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Promoções: cupons ----
@app.get("/api/shopee/cupons")
def shopee_cupons(status: str = "ongoing", user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_cupons(user.id, status=status)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/cupons")
def shopee_criar_cupom(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Body: {nome, codigo, inicio, fim, tipo_desconto(1=valor,2=%), valor, compra_minima, quantidade, escopo?}."""
    try:
        return shopee_campanhas.criar_cupom_verificado(user.id, payload)
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Faltam campos do cupom.")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.delete("/api/shopee/cupons/{voucher_id}")
def shopee_encerrar_cupom(voucher_id: str, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.encerrar_cupom(user.id, voucher_id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Shopee Ads ----
@app.get("/api/shopee/ads")
def shopee_ads(dias: int = 7, user: User = Depends(auth.get_current_user)):
    out = {}
    try:
        out["saldo"] = shopee.ads_saldo(user.id)
    except shopee.ShopeeError as e:
        out["saldo_erro"] = str(e)
    try:
        out["desempenho"] = shopee.ads_desempenho(user.id, dias=dias)
    except shopee.ShopeeError as e:
        out["desempenho_erro"] = str(e)
    return out


# ---- Q&A do anúncio ----
@app.get("/api/shopee/perguntas")
def shopee_perguntas(status: str = "UNANSWERED", user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_perguntas(user.id, status=status)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/perguntas/responder")
def shopee_responder_pergunta(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Responde uma pergunta do anúncio. {qa_id, texto, ia?, pergunta?}."""
    qid = payload.get("qa_id"); texto = payload.get("texto")
    if payload.get("ia") and not texto:
        prompt = ("Você é o vendedor respondendo a uma pergunta no anúncio (armarinho, Shopee Brasil). "
                  "Seja claro, cordial e direto, em português do Brasil. "
                  f"Pergunta do cliente: \"{payload.get('pergunta', '')}\". Responda SÓ com a resposta.")
        try:
            texto = ai._gerar_texto(user.id, prompt)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"IA falhou: {e}")
    if not qid or not texto:
        raise HTTPException(status_code=422, detail="Informe qa_id e texto (ou ia=true).")
    try:
        shopee.responder_pergunta(user.id, qid, texto)
        return {"ok": True, "texto": texto}
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Devoluções ----
@app.get("/api/shopee/devolucoes")
def shopee_devolucoes(dias: int = 30, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_devolucoes(user.id, dias=dias)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Divergência Bling × Shopee ----
@app.get("/api/shopee/divergencia")
def shopee_divergencia(user: User = Depends(auth.get_current_user)):
    """Margem real de cada anúncio Shopee × custo do Bling (por SKU): prejuízo, margem baixa
    ou saudável, com preço de equilíbrio e preço para a margem alvo."""
    try:
        return shopee.divergencia_bling_shopee(user.id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/item/preco")
def shopee_item_preco(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Ajusta o preço de um anúncio na Shopee (todas as variações). Body: {item_id, preco}."""
    try:
        return shopee.atualizar_preco_item(user.id, payload["item_id"], payload["preco"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Informe item_id e preco.")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/reprecificar")
def shopee_reprecificar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Ajuste de preço em lote na Shopee para a margem alvo. dry-run por padrão (mostra o plano).
    Body: { item_ids?: [int], aplicar: bool }. Respeita piso e pula SKUs em competição/sem custo."""
    item_ids = payload.get("item_ids")
    aplicar = bool(payload.get("aplicar", False))
    try:
        return shopee.reprecificar_shopee(user.id, item_ids=item_ids, aplicar=aplicar)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Bundle Deal ----
@app.get("/api/shopee/bundles")
def shopee_bundles(status: str = "ongoing", user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_bundles(user.id, status=status)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/bundles")
def shopee_criar_bundle(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Body: {nome, inicio, fim, rule_type(1=preço fixo,2=%,3=valor), valor, min_itens, item_ids:[]}."""
    try:
        res = shopee_campanhas.criar_bundle_verificado(user.id, payload)
        res["response"] = {"bundle_deal_id": res.get("bundle_deal_id")}
        if payload.get("item_ids") and not res.get("itens_adicionados"):
            motivo = (res.get("itens_recusados") or [{}])
            motivo = (motivo[0] or {}).get("motivo", "nenhum produto entrou no combo")
            raise HTTPException(status_code=502,
                detail=f"O combo foi criado, mas SEM produtos: {motivo}")
        return res
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Faltam campos do bundle.")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.delete("/api/shopee/bundles/{bundle_id}")
def shopee_encerrar_bundle(bundle_id: str, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.encerrar_bundle(user.id, bundle_id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Add-on Deal ----
@app.get("/api/shopee/addons")
def shopee_addons(status: str = "ongoing", user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_addons(user.id, status=status)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/addons")
def shopee_criar_addon(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Body: {nome, inicio, fim, principais:[item_id], adicionais:[{item_id, add_on_deal_price}]}."""
    try:
        res = shopee_campanhas.criar_addon_verificado(user.id, payload)
        res["response"] = {"add_on_deal_id": res.get("add_on_deal_id")}
        if payload.get("principais") and not res.get("principais_ok"):
            motivo = (res.get("itens_recusados") or [{}])
            motivo = (motivo[0] or {}).get("motivo", "o produto principal não pôde ser adicionado")
            raise HTTPException(status_code=502,
                detail=f"O add-on foi criado, mas sem o produto principal: {motivo}")
        return res
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Faltam campos do add-on.")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.delete("/api/shopee/addons/{addon_id}")
def shopee_encerrar_addon(addon_id: str, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.encerrar_addon(user.id, addon_id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ---- Flash Sale ----
@app.get("/api/shopee/flash/slots")
def shopee_flash_slots(dias: int = 14, user: User = Depends(auth.get_current_user)):
    try:
        return shopee_campanhas.slots_oficiais(user.id, dias=min(max(dias, 1), 30))
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/flash/{flash_sale_id}/habilitar")
def shopee_flash_habilitar(flash_sale_id: int, user: User = Depends(auth.get_current_user)):
    """Habilita os itens de uma Flash Sale existente e ativa a oferta (duas camadas)."""
    try:
        return shopee_campanhas.habilitar_flash_itens(user.id, flash_sale_id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/shopee/flash")
def shopee_flash(tipo: int = 1, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.listar_flash(user.id, tipo=tipo)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/shopee/flash")
def shopee_criar_flash(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Body: {timeslot_id, itens:[{item_id, purchase_limit, models:[...]}]} OU
    {timeslot_id, itens:[{item_id, desconto_pct, preco_promo, estoque}]} (monta o preço por variação)."""
    try:
        return shopee_campanhas.criar_flash_verificado(
            user.id, int(payload["timeslot_id"]), payload.get("itens", []),
            int(payload.get("reserva", 0)))
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="Informe timeslot_id e itens.")
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.delete("/api/shopee/flash/{flash_id}")
def shopee_encerrar_flash(flash_id: str, user: User = Depends(auth.get_current_user)):
    try:
        return shopee.encerrar_flash(user.id, flash_id)
    except shopee.ShopeeError as e:
        raise HTTPException(status_code=502, detail=str(e))


# ------------------------------- Produtos --------------------------------- #
@app.get("/api/produtos")
def listar_produtos(pagina: int = 1, limite: int = 100,
                    user: User = Depends(auth.get_current_user)):
    try:
        return bling.listar_produtos(user.id, pagina=pagina, limite=limite)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/produtos/{produto_id}")
def obter_produto(produto_id: int, user: User = Depends(auth.get_current_user)):
    """Detalhe limpo do produto + avaliação de preço por canal (motor de faixas)."""
    try:
        raw = bling.obter_produto(user.id, produto_id).get("data", {})
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    custo = float(raw.get("precoCusto") or (raw.get("fornecedor") or {}).get("precoCusto") or 0)
    preco = float(raw.get("preco") or 0)
    ncm = (raw.get("tributacao") or {}).get("ncm") or raw.get("ncm") or ""
    fotos = [i.get("link") for i in (((raw.get("midia") or {}).get("imagens") or {}).get("externas") or []) if i.get("link")]
    estoque = (raw.get("estoque") or {}).get("saldoVirtualTotal")
    dims = raw.get("dimensoes") or {}
    cfg = precificacao.obter_config(user.id)
    canais = []
    for c in cfg.get("canais", []):
        if not c.get("ativo"):
            continue
        av = precificacao.avaliar_com_cfg(cfg, custo, preco, c["canal"])
        canais.append({"canal": c["canal"], "nome": c["nome"],
                       "preco_sugerido": av["preco_sugerido"],
                       "margem_sugerida": av["margem_sugerida"],
                       "margem_atual": av["margem_atual"]})

    return {
        "id": raw.get("id"),
        "sku": raw.get("codigo"),
        "nome": raw.get("nome"),
        "preco": preco,
        "custo": custo,
        "ncm": ncm,
        "gtin": raw.get("gtin"),
        "peso_bruto": raw.get("pesoBruto"),
        "peso_liquido": raw.get("pesoLiquido"),
        "descricao_curta": raw.get("descricaoCurta"),
        "descricao_complementar": raw.get("descricaoComplementar"),
        "campos_customizados": [
            {"id": c.get("idCampoCustomizado"), "vinculo": c.get("idVinculo"),
             "rotulo": c.get("item") or "", "valor": c.get("valor")}
            for c in (raw.get("camposCustomizados") or [])
        ],
        "situacao": raw.get("situacao"),
        "tipo": raw.get("tipo"),
        "marca": raw.get("marca"),
        "unidade": raw.get("unidade"),
        "estoque": estoque,
        "dimensoes": {"largura": dims.get("largura"), "altura": dims.get("altura"),
                      "profundidade": dims.get("profundidade")} if dims else None,
        "fotos": fotos,
        "precificacao": canais,
        "qualidade": qualidade.score_cadastro({
            "nome": raw.get("nome"),
            "ean": raw.get("gtin"),
            "ncm": ncm,
            "peso": raw.get("pesoBruto") or raw.get("pesoLiquido"),
            "descricao": raw.get("descricaoComplementar") or raw.get("descricaoCurta"),
        }),
    }


_MAPA_PRODUTO = {  # nome amigável (front) -> campo da API do Bling
    "nome": "nome", "preco": "preco", "custo": "precoCusto", "ncm": "ncm",
    "gtin": "gtin", "peso_bruto": "pesoBruto", "peso_liquido": "pesoLiquido",
    "descricao_curta": "descricaoCurta", "descricao_complementar": "descricaoComplementar",
}


@app.put("/api/produtos/{produto_id}")
def atualizar_produto(produto_id: int, payload: dict = Body(...),
                      user: User = Depends(auth.get_current_user)):
    """Edita campos do produto no Bling (envia só o que veio no corpo) e registra o envio
    para o painel de sincronização (enviado -> confirmado quando o webhook voltar)."""
    campos = {_MAPA_PRODUTO[k]: v for k, v in payload.items()
              if k in _MAPA_PRODUTO and v is not None}
    if not campos:
        raise HTTPException(status_code=422, detail="Nenhum campo editável informado.")
    db = SessionLocal()
    try:
        try:
            bling.atualizar_produto(user.id, produto_id, campos)
        except bling.BlingAuthError as e:
            raise HTTPException(status_code=401, detail=str(e))
        sku = payload.get("sku") or payload.get("codigo")
        webhooks.registrar_envio(db, user.id, produto_id, sku, sorted(payload.keys()))
        if "precoCusto" in campos:  # reflete o novo custo no cache local (sem esperar o webhook)
            from .models import ProdutoCache
            pc = db.query(ProdutoCache).filter_by(user_id=user.id, produto_id=str(produto_id)).first()
            if pc:
                pc.custo = float(campos["precoCusto"] or 0)
                db.commit()
        if "preco" in campos:  # reflete o novo Preço Bling no cache local (sem esperar o webhook)
            from .models import ProdutoCache
            pc = db.query(ProdutoCache).filter_by(user_id=user.id, produto_id=str(produto_id)).first()
            if pc:
                pc.preco = float(campos["preco"] or 0)
                db.commit()
    finally:
        db.close()
    return {"ok": True, "atualizados": sorted(campos.keys())}


@app.get("/api/produtos/{produto_id}/status")
def produto_status(produto_id, user: User = Depends(auth.get_current_user)):
    """Status de sincronização (enviado/confirmado/pendente) + plataformas vinculadas do produto."""
    db = SessionLocal()
    try:
        sync = webhooks.status_sync(db, user.id, produto_id)
    finally:
        db.close()
    try:
        vinc = bling.vinculos_multiloja(user.id, produto_id)
    except Exception:  # noqa: BLE001
        vinc = []
    plataformas = [{"nome": v["nome"], "integracao": v.get("integracao"),
                    "publicado": v.get("publicado"), "ativo": v.get("ativo"),
                    "id_anuncio": v.get("id_anuncio"), "preco": v.get("preco")} for v in vinc]
    return {"produto_id": produto_id, "sync": sync, "plataformas": plataformas,
            "fonte_canais": bool(vinc)}


# ---------- Diagnóstico: JSON cru do Bling (p/ construir telas sobre dado real) ----------
@app.get("/api/diagnostico/precos/{produto_id}")
def diag_precos(produto_id, user: User = Depends(auth.get_current_user)):
    """Payload CRU do produto + tabelas de preço — revela onde o Bling guarda o preço por canal."""
    try:
        prod = (bling.obter_produto(user.id, produto_id) or {}).get("data", {})
        try:
            tabelas = bling.listar_tabelas_precos(user.id)
        except Exception as e:  # noqa: BLE001
            tabelas = {"erro": str(e)}
        return {"produto_id": produto_id,
                "preco_base": prod.get("preco"),
                "tem_variacoes": bool(prod.get("variacoes")),
                "chaves_produto": sorted(prod.keys()),
                "tabelas_precos": tabelas}
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/produtos/{produto_id}/sincronizacao")
def produto_sincronizacao(produto_id, user: User = Depends(auth.get_current_user)):
    """Preço-alvo por canal (preserva o líquido) vs. preço registrado no Bling por marketplace.
    Lê os vínculos multiloja. Canais não configurados aparecem só com o registrado + flag de
    prejuízo (preço abaixo do líquido)."""
    try:
        raw = (bling.obter_produto(user.id, produto_id) or {}).get("data", {}) or {}
        vinc = bling.vinculos_multiloja(user.id, produto_id)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    base = float(raw.get("preco") or 0)
    cfg = precificacao.obter_config(user.id)
    # Shopee: temos API direta — o cache da Shopee é a fonte da verdade do "publicado",
    # independente do vínculo do Bling (que a v3 não devolve com preço).
    shopee_cache = None
    sku_prod = raw.get("codigo")
    if sku_prod:
        try:
            from .models import ShopeeItemCache
            with SessionLocal() as _db:
                shopee_cache = (_db.query(ShopeeItemCache)
                                .filter_by(user_id=user.id, sku=sku_prod)
                                .order_by(ShopeeItemCache.atualizado_em.desc()).first())
        except Exception:  # noqa: BLE001
            shopee_cache = None
    # Mercado Livre: API direta (igual à Shopee). Dormente até as credenciais existirem —
    # configurado() é False sem ML_CLIENT_ID/SECRET/REFRESH_TOKEN, então isto vira no-op.
    ml_item = None
    if sku_prod:
        try:
            from . import mercadolivre as _ml
            if _ml.configurado(user.id):
                ml_item = _ml.buscar_item_por_sku(sku_prod, user.id)
        except Exception:  # noqa: BLE001
            ml_item = None
    precos = {v["canal"]: v["preco"] for v in vinc if v.get("canal") and v["preco"] > 0}
    canais = precificacao.divergencias(cfg, base, precos)
    alvo_por_canal = {c["canal"]: c.get("preco_alvo") for c in canais}
    vinculos = [{
        "nome": v["nome"], "integracao": v["integracao"], "canal": v.get("canal"),
        "id_loja": v.get("id_loja"), "id_anuncio": v.get("id_anuncio"),
        "preco_registrado": v["preco"], "link": v.get("link"),
        "publicado": v.get("publicado"), "ativo": v.get("ativo"),
        "preco_alvo": alvo_por_canal.get(v.get("canal")),
        "prejuizo": bool(v["preco"] > 0 and base > 0 and v["preco"] < base),
    } for v in vinc]

    # Painel por canal: TODOS os canais ativos do config, com líquido realizado + status,
    # mesclando os vínculos existentes. Alimenta a tabela e os badges do cockpit.
    vinc_por_canal = {}
    for v in vinc:
        cn = v.get("canal")
        if not cn:
            continue
        cur = vinc_por_canal.get(cn)
        # com duas lojas no mesmo canal (ex.: duas Shopee), fica com a que tem anúncio/preço
        if cur is None or (not (cur.get("id_anuncio") or cur.get("preco")) and (v.get("id_anuncio") or v.get("preco"))):
            vinc_por_canal[cn] = v

    def _liq_canal(faixas, preco):
        if not preco or preco <= 0:
            return None
        fx = precificacao._faixa_para_preco(faixas, preco) if faixas else {}
        taxa = preco * (float(fx.get("comissao") or 0) + float(fx.get("fixo_pct") or 0)) / 100.0 + float(fx.get("fixo") or 0)
        return round(preco - taxa - preco * float(cfg.get("imposto") or 0) / 100.0 - float(cfg.get("embalagem") or 0), 2)

    # id_loja por canal a partir das lojas conhecidas da conta. É o que garante que o
    # "Aplicar" SEMPRE grava no Bling (hub/fonte da verdade), mesmo quando a v3 não
    # devolve o vínculo com preço — senão a próxima sincronização propaga o preço antigo.
    lojas_por_canal = {}
    try:
        for lj, meta in bling.lojas_da_conta(user.id).items():
            cn_l = bling.MAPA_INTEGRACAO.get((meta.get("integracao") or "").lower())
            if cn_l and cn_l not in lojas_por_canal:
                lojas_por_canal[cn_l] = lj
    except Exception:  # noqa: BLE001
        lojas_por_canal = {}

    canais_painel = []
    cobertos = set()
    for c in (cfg.get("canais") or []):
        cn = c.get("canal")
        v = vinc_por_canal.get(cn)
        # publicado = o produto está vinculado/anunciado nesse canal (independe de termos o preço)
        publicado = bool(v and (v.get("publicado") or v.get("id_anuncio") or v.get("ativo") or v.get("preco")))
        preco_reg = float(v["preco"]) if (v and v.get("preco") and v["preco"] > 0) else None
        item_id = None
        # Shopee: cache direto manda. Marca publicado e completa preço/item_id.
        if cn == "shopee" and shopee_cache:
            publicado = True
            item_id = shopee_cache.item_id
            if preco_reg is None:
                cp = float(shopee_cache.preco_original or 0) or float(shopee_cache.preco or 0)
                if cp > 0:
                    preco_reg = cp
        # Mercado Livre: API direta. Marca publicado e completa preço/anúncio.
        if cn == "mercadolivre" and ml_item:
            publicado = True
            if not item_id:
                item_id = ml_item.get("item_id")
            if preco_reg is None:
                mp = float(ml_item.get("preco_original") or 0) or float(ml_item.get("preco") or 0)
                if mp > 0:
                    preco_reg = mp
        # mostra o canal se está ativo na config OU se o produto já está anunciado nele
        if not c.get("ativo") and not publicado:
            continue
        liquido = _liq_canal(c.get("faixas") or [], preco_reg) if preco_reg else None
        if not publicado:
            status = "falta_anunciar"
        elif preco_reg is None or liquido is None:
            status = "sem_preco"
        elif liquido < 0:
            status = "prejuizo"
        elif base > 0 and liquido < base - 0.01:
            status = "abaixo"
        else:
            status = "no_padrao"
        canais_painel.append({
            "canal": cn, "nome": c.get("nome") or cn, "publicado": publicado,
            "preco_registrado": preco_reg, "preco_alvo": alvo_por_canal.get(cn),
            "liquido": liquido, "status": status, "ativo_cfg": bool(c.get("ativo")),
            "id_loja": (v.get("id_loja") if v else None) or lojas_por_canal.get(cn),
            "id_anuncio": (v.get("id_anuncio") if v else None) or item_id,
            "item_id": item_id,
        })
        cobertos.add(cn)

    # Canais onde o produto ESTÁ anunciado mas nem constam na config de precificação.
    # Aparecem como publicados (pra não dizer "Anunciar" indevidamente); o líquido fica
    # pendente até cadastrar as taxas do canal na configuração.
    NOMES_CANAL = {"mercadolivre": "Mercado Livre", "shopee": "Shopee", "shein": "Shein",
                   "tiktok": "TikTok", "nuvemshop": "Nuvemshop", "amazon": "Amazon",
                   "magalu": "Magalu", "americanas": "Americanas", "tray": "Tray", "shopify": "Shopify"}
    for v in vinc:
        cn = v.get("canal") or ((v.get("integracao") or "").strip().lower() or None)
        if not cn or cn in cobertos:
            continue
        if not (v.get("publicado") or v.get("id_anuncio") or v.get("ativo") or v.get("preco")):
            continue
        preco_reg = float(v["preco"]) if (v.get("preco") and v["preco"] > 0) else None
        canais_painel.append({
            "canal": cn, "nome": NOMES_CANAL.get(cn) or v.get("nome") or cn,
            "publicado": True, "preco_registrado": preco_reg,
            "preco_alvo": alvo_por_canal.get(cn), "liquido": None,
            "status": "sem_preco" if preco_reg is None else "sem_taxas", "ativo_cfg": False,
            "id_loja": v.get("id_loja") or lojas_por_canal.get(cn),
            "id_anuncio": v.get("id_anuncio"), "item_id": None,
        })
        cobertos.add(cn)

    return {"produto_id": raw.get("id"), "sku": raw.get("codigo"),
            "base_venda": round(base, 2), "canais": canais,
            "canais_painel": canais_painel,
            "vinculos": vinculos, "fonte_lida": bool(vinc)}


@app.get("/api/produtos/{produto_id}/simular")
def produto_simular_liquido(produto_id, canal: str, preco: float,
                            user: User = Depends(auth.get_current_user)):
    """Simula o líquido e a margem de um preço hipotético num canal (sem gravar nada).
    Usa as faixas/taxas do canal na config + imposto + embalagem + custo do produto.
    Alimenta o cálculo ao vivo da Promoção e da edição manual por canal."""
    try:
        raw = (bling.obter_produto(user.id, produto_id) or {}).get("data", {}) or {}
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    cfg = precificacao.obter_config(user.id)
    base = float(raw.get("preco") or 0)
    custo = float(raw.get("precoCusto") or (raw.get("fornecedor") or {}).get("precoCusto") or 0)
    canal_cfg = next((c for c in (cfg.get("canais") or []) if c.get("canal") == canal), None)
    faixas = (canal_cfg or {}).get("faixas") or []
    if not preco or preco <= 0:
        return {"canal": canal, "preco": preco, "liquido": None, "margem": None,
                "taxa": None, "custo": round(custo, 2), "alvo": round(base, 2), "abaixo_alvo": False}
    fx = precificacao._faixa_para_preco(faixas, preco) if faixas else {}
    fx = fx or {}
    comissao_pct = float(fx.get("comissao") or 0) + float(fx.get("fixo_pct") or 0)
    taxa_fixa = float(fx.get("fixo") or 0)
    taxa = preco * comissao_pct / 100.0 + taxa_fixa
    imp_pct = float(cfg.get("imposto") or 0)
    imp_val = preco * imp_pct / 100.0
    emb = float(cfg.get("embalagem") or 0)
    liquido = round(preco - taxa - imp_val - emb, 2)
    margem = round((liquido - custo) / liquido * 100, 1) if (custo > 0 and liquido) else None
    # quebra do líquido (cascata "anatomia do líquido"): cada dedução do preço de lista
    quebra = []
    if taxa:
        rot = f"Taxa {canal} {comissao_pct:.0f}%".strip() if comissao_pct else "Taxa fixa do canal"
        quebra.append({"rotulo": rot, "valor": -round(taxa, 2)})
    if imp_val:
        quebra.append({"rotulo": f"Imposto {imp_pct:.0f}%", "valor": -round(imp_val, 2)})
    if emb:
        quebra.append({"rotulo": "Embalagem / custo fixo", "valor": -round(emb, 2)})
    return {"canal": canal, "preco": round(preco, 2), "liquido": liquido, "taxa": round(taxa, 2),
            "custo": round(custo, 2), "margem": margem, "alvo": round(base, 2),
            "lucro": round(liquido - custo, 2) if custo > 0 else None,
            "imposto_pct": imp_pct, "comissao_pct": comissao_pct,
            "quebra": quebra, "abaixo_alvo": bool(base > 0 and liquido < base - 0.01),
            "tem_faixas": bool(faixas)}


@app.get("/api/produtos/{produto_id}/mercadolivre")
def produto_mercadolivre(produto_id, user: User = Depends(auth.get_current_user)):
    """Snapshot direto do Mercado Livre para um produto — alimenta os cartões de Funil
    (visitas reais) e Radar (preço de referência) da Visão geral do cockpit, mais a
    tarifa real da categoria. Tudo best-effort: se algo falhar, volta nulo e o cockpit segue."""
    from . import mercadolivre as ml
    if not ml.configurado(user.id):
        return {"conectado": False}
    try:
        raw = (bling.obter_produto(user.id, produto_id) or {}).get("data", {}) or {}
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    sku = raw.get("codigo")
    if not sku:
        return {"conectado": True, "item": None}
    item = None
    try:
        item = ml.cache_por_sku(user.id, sku)
    except Exception:  # noqa: BLE001
        item = None
    if not item:
        try:
            it = ml.buscar_item_por_sku(sku, user.id)
            if it:
                item = {"item_id": it.get("item_id"), "titulo": it.get("titulo"),
                        "preco": it.get("preco"), "status": it.get("status"),
                        "estoque": it.get("estoque"), "permalink": it.get("permalink"),
                        "category_id": it.get("category_id"),
                        "listing_type_id": it.get("listing_type_id"),
                        "logistic_type": it.get("logistic_type")}
        except Exception:  # noqa: BLE001
            item = None
    if not item or not item.get("item_id"):
        return {"conectado": True, "item": None}
    item_id = item["item_id"]

    radar = None
    try:
        r = ml.referencia_de_preco(item_id, user.id)
        radar = {"atual": r.get("atual"), "sugerido": r.get("sugerido"),
                 "menor": r.get("menor"), "diff_pct": r.get("diff_pct"),
                 "n_concorrentes": len(r.get("concorrentes") or [])}
    except Exception:  # noqa: BLE001
        radar = None

    visitas = None
    try:
        v = ml.visitas_do_item(item_id, last=30, unit="day", user_id=user.id) or {}
        total = v.get("total_visits")
        if total is None and isinstance(v.get("results"), list):
            total = sum(int((p or {}).get("total") or 0) for p in v["results"])
        visitas = {"total": total, "dias": 30}
    except Exception:  # noqa: BLE001
        visitas = None

    perguntas = None
    try:
        perguntas = ml.listar_perguntas(user.id, status="UNANSWERED",
                                         item_id=item_id, limit=1).get("total")
    except Exception:  # noqa: BLE001
        perguntas = None

    tarifa = None
    try:
        if item.get("category_id") and item.get("preco"):
            t = ml.tarifas_de_venda(item.get("category_id"), item.get("preco"),
                                    item.get("listing_type_id") or "gold_special",
                                    item.get("logistic_type"), user_id=user.id)
            tarifa = {"comissao_pct": t.get("comissao_pct"), "sale_fee": t.get("sale_fee"),
                      "custo_fixo": t.get("custo_fixo")}
    except Exception:  # noqa: BLE001
        tarifa = None

    return {"conectado": True, "item": item, "radar": radar,
            "visitas": visitas, "tarifa_real": tarifa,
            "perguntas_sem_resposta": perguntas}


@app.get("/api/produtos/{produto_id}/qualidade")
def produto_qualidade(produto_id, user: User = Depends(auth.get_current_user)):
    """Diagnóstico de qualidade do anúncio: funde o anúncio da Shopee (fotos, vídeo,
    descrição) com a ficha do Bling (título, EAN, NCM, peso). Devolve nota 0-100,
    componentes com status e um plano priorizado. Tudo dado real — nada inventado."""
    try:
        raw = (bling.obter_produto(user.id, produto_id) or {}).get("data", {}) or {}
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    def _dig(v):
        return "".join(c for c in str(v or "") if c.isdigit())

    nome = (raw.get("nome") or "").strip()
    sku = raw.get("codigo")
    ean = raw.get("gtin") or raw.get("gtinEmbalagem") or ""
    ncm = raw.get("ncm") or (raw.get("tributacao") or {}).get("ncm") or ""
    peso = float(raw.get("pesoBruto") or raw.get("pesoLiquido") or 0)
    desc_bling = (raw.get("descricaoCurta") or raw.get("descricaoComplementar")
                  or raw.get("observacoes") or "")

    # --- anúncio Shopee (fotos, vídeo, descrição, título) ---
    fotos = None
    tem_video = False
    desc_shopee = ""
    titulo_shopee = ""
    tem_shopee = False
    if sku:
        try:
            from .models import ShopeeItemCache
            with SessionLocal() as _db:
                cache = (_db.query(ShopeeItemCache)
                         .filter_by(user_id=user.id, sku=sku)
                         .order_by(ShopeeItemCache.atualizado_em.desc()).first())
            if cache and cache.item_id:
                info = shopee.info_itens(user.id, [cache.item_id])
                lst = ((info.get("response") or {}).get("item_list") or [])
                if lst:
                    it = lst[0]
                    tem_shopee = True
                    img = (it.get("image") or {})
                    fotos = len(img.get("image_url_list") or img.get("image_id_list") or [])
                    vid = it.get("video_info") or it.get("video_upload_id") or []
                    tem_video = bool(vid)
                    desc_shopee = it.get("description") or ""
                    titulo_shopee = it.get("item_name") or ""
        except Exception:  # noqa: BLE001
            tem_shopee = False

    titulo = titulo_shopee or nome
    desc = desc_shopee if len(desc_shopee) >= len(desc_bling) else desc_bling
    len_tit = len(titulo)
    len_desc = len(desc)

    componentes = []
    score = 0.0

    # Título (peso 20)
    if len_tit >= 40:
        s, st = 20, "ok"
    elif len_tit >= 30:
        s, st = 15, "ok"
    elif len_tit >= 15:
        s, st = 8, "atencao"
    else:
        s, st = 0, "falta"
    score += s
    componentes.append({"chave": "titulo", "label": "Título", "valor": s, "max": 20, "status": st,
                        "detalhe": f"{len_tit} caracteres" + (" · com palavras-chave" if len_tit >= 40 else ""),
                        "acao": None if st == "ok" else "Use 40+ caracteres com material e medida."})

    # Fotos (peso 25) — só Shopee tem essa info
    if tem_shopee and fotos is not None:
        s = round(25 * min(fotos, 9) / 9)
        st = "ok" if fotos >= 9 else ("atencao" if fotos >= 4 else "falta")
        det = f"{fotos}/9 fotos"
        ac = None if fotos >= 9 else f"Adicione mais {9 - fotos} — anúncios com 9 fotos vendem mais."
    else:
        s, st, det, ac = 0, "sem_dados", "sem anúncio Shopee", "Vincule/sincronize a Shopee pra ler as fotos."
    score += s
    componentes.append({"chave": "fotos", "label": "Fotos", "valor": s, "max": 25, "status": st,
                        "detalhe": det, "acao": ac, "fotos": fotos})

    # Atributos da ficha (peso 20): EAN + NCM + peso
    faltam = []
    if len(_dig(ean)) not in (8, 12, 13, 14):
        faltam.append("EAN/GTIN")
    if len(_dig(ncm)) != 8:
        faltam.append("NCM")
    if peso <= 0:
        faltam.append("peso")
    ok_attr = 3 - len(faltam)
    s = round(20 * ok_attr / 3)
    st = "ok" if not faltam else ("atencao" if ok_attr >= 1 else "falta")
    score += s
    componentes.append({"chave": "atributos", "label": "Atributos", "valor": s, "max": 20, "status": st,
                        "detalhe": "completos" if not faltam else f"faltam {len(faltam)}: {', '.join(faltam)}",
                        "acao": None if not faltam else f"Preencha: {', '.join(faltam)}."})

    # Descrição (peso 20)
    if len_desc >= 200:
        s, st = 20, "ok"
    elif len_desc >= 80:
        s, st = 12, "atencao"
    else:
        s, st = 0, "falta"
    score += s
    componentes.append({"chave": "descricao", "label": "Descrição", "valor": s, "max": 20, "status": st,
                        "detalhe": f"{len_desc} caracteres" + (" · completa" if len_desc >= 200 else ""),
                        "acao": None if st == "ok" else "Descreva medidas e benefícios (200+ caracteres)."})

    # Vídeo (peso 15)
    if tem_video:
        s, st = 15, "ok"
    elif tem_shopee:
        s, st = 0, "falta"
    else:
        s, st = 0, "sem_dados"
    score += s
    componentes.append({"chave": "video", "label": "Vídeo", "valor": s, "max": 15, "status": st,
                        "detalhe": "tem vídeo" if tem_video else ("sem vídeo" if tem_shopee else "sem anúncio Shopee"),
                        "acao": None if tem_video else ("Anúncio com vídeo converte mais e aparece no feed." if tem_shopee else None)})

    score = int(round(score))
    if score >= 85:
        label = "Excelente"
    elif score >= 70:
        label = "Bom — dá pra subir pra Excelente"
    elif score >= 50:
        label = "Regular — vale completar"
    else:
        label = "Precisa de atenção"

    # plano priorizado (maior ganho primeiro)
    plano = sorted([c for c in componentes if c["status"] in ("falta", "atencao", "sem_dados") and c.get("acao")],
                   key=lambda c: (c["max"] - c["valor"]), reverse=True)
    plano = [{"label": c["label"], "acao": c["acao"], "ganho": c["max"] - c["valor"]} for c in plano]
    potencial = min(100, score + sum(p["ganho"] for p in plano))

    return {"score": score, "label": label, "potencial": potencial,
            "tem_shopee": tem_shopee, "componentes": componentes, "plano": plano}


@app.post("/api/catalogo/kpi-snapshot")
def catalogo_kpi_snapshot(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Upsert da foto de hoje dos KPIs do catálogo (1 linha por dia). O front manda os
    números já calculados; aqui só guardamos pra alimentar tendência/sparkline."""
    from datetime import date as _date
    from .models import KpiSnapshot

    def _i(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    def _f(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    if _i(payload.get("total")) <= 0:
        return {"ok": False, "motivo": "catálogo ainda não carregado"}
    hoje = _date.today()
    with SessionLocal() as db:
        row = db.query(KpiSnapshot).filter_by(user_id=user.id, dia=hoje).first()
        if not row:
            row = KpiSnapshot(user_id=user.id, dia=hoje)
            db.add(row)
        row.total = _i(payload.get("total"))
        row.saudavel = _i(payload.get("saud"))
        row.atencao = _i(payload.get("aten"))
        row.prejuizo = _i(payload.get("prej"))
        row.sem_custo = _i(payload.get("semCusto"))
        row.val_estoque = float(payload.get("valEstoque") or 0)
        row.marg_media = _f(payload.get("margMedia"))
        row.cobertura = payload.get("cobertura") or []
        row.criado_em = datetime.utcnow()
        db.commit()
    return {"ok": True, "dia": hoje.isoformat()}


@app.get("/api/catalogo/kpi-historico")
def catalogo_kpi_historico(dias: int = 30, user: User = Depends(auth.get_current_user)):
    """Série diária dos KPIs pra desenhar tendência/sparkline no topo do Catálogo."""
    from datetime import date as _date, timedelta as _td
    from .models import KpiSnapshot
    desde = _date.today() - _td(days=int(dias))
    with SessionLocal() as db:
        rows = (db.query(KpiSnapshot)
                .filter(KpiSnapshot.user_id == user.id, KpiSnapshot.dia >= desde)
                .order_by(KpiSnapshot.dia.asc()).all())
        pontos = [{"dia": r.dia.isoformat(), "total": r.total, "saud": r.saudavel,
                   "aten": r.atencao, "prej": r.prejuizo, "sem_custo": r.sem_custo,
                   "val_estoque": r.val_estoque, "marg_media": r.marg_media,
                   "cobertura": r.cobertura} for r in rows]
    return {"pontos": pontos}


@app.get("/api/mercadolivre/status")
def mercadolivre_status(user: User = Depends(auth.get_current_user)):
    """Status da integração direta com o Mercado Livre (mesma ideia da Shopee)."""
    from . import mercadolivre as ml
    return {"configurado": ml.configurado()}


@app.get("/api/mercadolivre/item")
def mercadolivre_item(sku: str, user: User = Depends(auth.get_current_user)):
    """Lê o anúncio do Mercado Livre por SKU (preço/status), via API direta do ML."""
    from . import mercadolivre as ml
    if not ml.configurado():
        return {"configurado": False, "item": None}
    try:
        return {"configurado": True, "item": ml.buscar_item_por_sku(sku)}
    except ml.MLNaoConfigurado:
        return {"configurado": False, "item": None}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Mercado Livre: {e}")


_HTML_ML_OK = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>Mercado Livre conectado</title></head>
<body style="font-family:system-ui,sans-serif;padding:2.5rem;max-width:640px;margin:auto;background:#0b0b0f;color:#eee">
<div style="font-size:40px;text-align:center">✅</div>
<h2 style="color:#FFE600;text-align:center;margin-top:.3rem">Mercado Livre autorizado{nick_sufixo}</h2>
<p style="color:#aaa;text-align:center">Copie os dois valores abaixo e cole no Railway (variáveis de ambiente do backend), depois faça um deploy.</p>
<div style="margin-top:1.4rem">
<div style="color:#888;font-size:12px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:.3rem">ML_REFRESH_TOKEN</div>
<div style="background:#16161c;border:1px solid #333;border-radius:8px;padding:.7rem .9rem;word-break:break-all;font-family:monospace;font-size:13px;color:#9ad">{refresh}</div>
</div>
<div style="margin-top:1rem">
<div style="color:#888;font-size:12px;text-transform:uppercase;letter-spacing:.05em;margin-bottom:.3rem">ML_SELLER_ID</div>
<div style="background:#16161c;border:1px solid #333;border-radius:8px;padding:.7rem .9rem;font-family:monospace;font-size:13px;color:#9ad">{sid}</div>
</div>
<p style="color:#666;font-size:12px;margin-top:1.4rem">O ML_CLIENT_ID e o ML_CLIENT_SECRET você já colocou no Railway. Com esses quatro, o Catálogo passa a ler e atualizar o preço do Mercado Livre automaticamente. Pode fechar esta aba.</p>
</body></html>"""


def _ml_redirect_uri(request: Request) -> str:
    import os
    env = os.environ.get("ML_REDIRECT_URI")
    if env:
        return env.rstrip("/")
    return f"{_shopee_redirect_base(request)}/api/mercadolivre/callback"


@app.get("/api/mercadolivre/conectar")
def mercadolivre_conectar(request: Request):
    """Abra esta URL no navegador pra autorizar o app no Mercado Livre. Leva pro login
    do ML e volta no /callback mostrando o refresh_token + seller_id."""
    from fastapi.responses import RedirectResponse
    from . import mercadolivre as ml
    try:
        url = ml.url_autorizacao(_ml_redirect_uri(request))
    except ml.MLNaoConfigurado as e:
        return HTMLResponse(_HTML_ERR.format(msg=f"{e}. Defina ML_CLIENT_ID no Railway e faça deploy."), status_code=400)
    return RedirectResponse(url)


@app.get("/api/mercadolivre/callback")
def mercadolivre_callback(request: Request, code: str = "", error: str = "", state: str = ""):
    """Callback único do OAuth do ML (redirect fixo, casa com o cadastro do app).
    Com 'state' válido: salva a conta do tenant no banco e fecha o popup. Sem 'state':
    modo setup por ambiente — mostra refresh_token/seller_id pra colar no Railway."""
    from . import mercadolivre as ml
    if error:
        return HTMLResponse(_HTML_ERR.format(msg=f"Mercado Livre recusou: {error}"), status_code=400)
    if not code:
        return HTMLResponse(_HTML_ERR.format(msg="Autorização não recebida do Mercado Livre."), status_code=400)
    try:
        d = ml.trocar_code_por_token(code, _ml_redirect_uri(request))
        refresh = d.get("refresh_token") or ""
        access = d.get("access_token") or ""
        sid, nick = "", ""
        try:
            me = ml.conta_do_token(access)
            sid = str(me.get("id") or "")
            nick = me.get("nickname") or ""
        except Exception:  # noqa: BLE001
            pass
        uid = ml.ler_state(state) if state else None
        if uid:  # multi-tenant (popup): grava a conta e encerra
            ml.salvar_conta(uid, refresh, access, d.get("expires_in") or 21600,
                            seller_id=sid, nickname=nick)
            return HTMLResponse(_HTML_OK)
        nick_sufixo = f" · {nick}" if nick else ""
        return HTMLResponse(_HTML_ML_OK.format(refresh=refresh, sid=sid, nick_sufixo=nick_sufixo))
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(_HTML_ERR.format(msg=str(e)), status_code=400)


# =========================== Mercado Livre — Enterprise =========================== #
def _ml_run(call):
    """Roda uma chamada do módulo ML e converte exceções em HTTPException."""
    from . import mercadolivre as ml
    try:
        return call(ml)
    except ml.MLNaoConfigurado as e:
        raise HTTPException(status_code=400, detail=f"Mercado Livre não conectado: {e}")
    except ml.MLErro as e:
        raise HTTPException(status_code=502, detail=f"Mercado Livre: {e}")


# --- Conta / conexão ---
@app.get("/api/mercadolivre/conta")
def ml_conta(user: User = Depends(auth.get_current_user)):
    from . import mercadolivre as ml
    st = ml.status_conexao(user.id)
    if st.get("conta"):
        try:
            st["reputacao"] = ml.reputacao(user.id)
        except Exception:  # noqa: BLE001
            pass
    return st


@app.get("/api/mercadolivre/limites")
def ml_limites(user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.limites_publicacao(user.id))


@app.get("/api/mercadolivre/reputacao")
def ml_reputacao(user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.reputacao(user.id))


def _cor_nivel(level_id):
    l = (level_id or "").lower()
    if "green" in l:
        return "verde"
    if "yellow" in l:
        return "amarelo"
    if "orange" in l:
        return "laranja"
    if "red" in l:
        return "vermelho"
    return None


_TIER_LABEL = {"platinum": "Mercado Líder Platinum", "gold": "Mercado Líder Gold",
               "silver": "Mercado Líder"}


def _metrica(m, meta):
    """Normaliza uma métrica do ML (rate fração -> %) com meta e veredito."""
    if not isinstance(m, dict):
        return None
    rate = m.get("rate")
    pct = round(float(rate) * 100, 2) if rate is not None else None
    return {"rate": pct, "valor": m.get("value"), "periodo": m.get("period"),
            "meta": meta, "ok": (pct is not None and pct <= meta)}


@app.get("/api/mercadolivre/reputacao/painel")
def ml_reputacao_painel(user: User = Depends(auth.get_current_user)):
    """Reputação do vendedor normalizada para o painel (nível/cor, tier, métricas)."""
    from . import mercadolivre as ml
    try:
        rep = ml.reputacao(user.id)
    except ml.MLNaoConfigurado:
        return {"conectado": False}
    except ml.MLErro as e:
        return {"conectado": False, "erro": str(e)[:200]}
    if not rep or not (rep.get("nivel") or rep.get("metricas") or rep.get("transacoes")):
        return {"conectado": True, "sem_dados": True}
    nivel = rep.get("nivel")
    cor = _cor_nivel(nivel)
    nivel_num = None
    for ch in (nivel or ""):
        if ch.isdigit():
            nivel_num = int(ch)
            break
    tier = rep.get("status")
    metr = rep.get("metricas") or {}
    trans = rep.get("transacoes") or {}
    ratings = trans.get("ratings") or {}
    return {
        "conectado": True,
        "nivel": nivel, "nivel_num": nivel_num, "cor": cor,
        "tier": tier, "tier_label": _TIER_LABEL.get(tier), "eh_lider": bool(tier),
        "metricas": {
            "reclamacoes": _metrica(metr.get("claims"), 3.0),
            "envio_atrasado": _metrica(metr.get("delayed_handling_time"), 15.0),
            "cancelamentos": _metrica(metr.get("cancellations"), 3.0),
            "vendas": {"completadas": (metr.get("sales") or {}).get("completed"),
                       "periodo": (metr.get("sales") or {}).get("period")},
        },
        "transacoes": {
            "total": trans.get("total"), "completadas": trans.get("completed"),
            "canceladas": trans.get("canceled"),
            "positivas": ratings.get("positive"), "neutras": ratings.get("neutral"),
            "negativas": ratings.get("negative"),
        },
    }


# --- Conexão multi-tenant (popup, salva a conta no banco) ---
@app.get("/api/mercadolivre/auth/login")
def ml_auth_login(request: Request, user: User = Depends(auth.get_current_user)):
    from . import mercadolivre as ml
    if not ml.app_configurado():
        raise HTTPException(status_code=400, detail="App ML não configurado (ML_CLIENT_ID/SECRET).")
    state = ml.state_token(user.id)
    redirect = _ml_redirect_uri(request)  # FIXO: /api/mercadolivre/callback — casa com o redirect cadastrado no app do ML
    return {"url": ml.url_autorizacao(redirect, state=state)}


@app.get("/api/mercadolivre/auth/callback/{state}")
def ml_auth_callback(state: str, request: Request, code: str = "", error: str = ""):
    from . import mercadolivre as ml
    uid = ml.ler_state(state)
    if not uid:
        return HTMLResponse(_HTML_ERR.format(msg="Sessão de conexão expirada. Tente de novo."), status_code=400)
    if error or not code:
        return HTMLResponse(_HTML_ERR.format(msg="Autorização não recebida do Mercado Livre."), status_code=400)
    try:
        redirect = f"{_shopee_redirect_base(request)}/api/mercadolivre/auth/callback/{state}"
        d = ml.trocar_code_por_token(code, redirect)
        access = d.get("access_token") or ""
        sid, nick = "", ""
        try:
            me = ml.conta_do_token(access)
            sid = str(me.get("id") or "")
            nick = me.get("nickname") or ""
        except Exception:  # noqa: BLE001
            pass
        ml.salvar_conta(uid, d.get("refresh_token") or "", access,
                        d.get("expires_in") or 21600, seller_id=sid, nickname=nick)
        return HTMLResponse(_HTML_OK)
    except Exception as e:  # noqa: BLE001
        return HTMLResponse(_HTML_ERR.format(msg=str(e)), status_code=400)


# --- Sincronização do catálogo (job pesado, em background) ---
@app.post("/api/mercadolivre/sincronizar")
def ml_sincronizar(background_tasks: BackgroundTasks, user: User = Depends(auth.get_current_user)):
    from . import mercadolivre as ml
    if not ml.configurado(user.id):
        raise HTTPException(status_code=400, detail="Conecte a conta do Mercado Livre primeiro.")
    background_tasks.add_task(ml.sincronizar_catalogo, user.id)
    return {"ok": True, "msg": "Sincronização iniciada."}


@app.get("/api/mercadolivre/sync")
def ml_sync_status(user: User = Depends(auth.get_current_user)):
    from . import mercadolivre as ml
    return ml.status_sync(user.id)


# --- Catálogo ---
@app.get("/api/mercadolivre/itens")
def ml_itens(sku: str = "", user: User = Depends(auth.get_current_user)):
    from . import mercadolivre as ml
    return {"itens": ml.listar_cache(user.id, sku=sku or None)}


@app.get("/api/mercadolivre/item-id/{item_id}")
def ml_item_por_id(item_id: str, user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.obter_item(item_id, user.id))


@app.get("/api/mercadolivre/descricao/{item_id}")
def ml_descricao(item_id: str, user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: {"texto": ml.descricao_item(item_id, user.id)})


# --- Preço / status / estoque do anúncio ---
@app.post("/api/mercadolivre/preco")
def ml_preco(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    item_id = payload.get("item_id")
    preco = payload.get("preco")
    if not item_id or preco is None:
        raise HTTPException(status_code=422, detail="Informe item_id e preco.")
    return _ml_run(lambda ml: ml.atualizar_preco(item_id, float(preco), user.id))


@app.post("/api/mercadolivre/anuncio-status")
def ml_anuncio_status(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    item_id = payload.get("item_id")
    status = payload.get("status")
    if not item_id or status not in ("active", "paused", "closed"):
        raise HTTPException(status_code=422, detail="Informe item_id e status (active|paused|closed).")
    return _ml_run(lambda ml: ml.atualizar_status(item_id, status, user.id))


@app.post("/api/mercadolivre/anuncio-estoque")
def ml_anuncio_estoque(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    item_id = payload.get("item_id")
    qtd = payload.get("qtd")
    if not item_id or qtd is None:
        raise HTTPException(status_code=422, detail="Informe item_id e qtd.")
    return _ml_run(lambda ml: ml.atualizar_estoque(item_id, int(qtd), user.id))


@app.post("/api/mercadolivre/descricao")
def ml_set_descricao(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    item_id = (payload or {}).get("item_id")
    texto = ((payload or {}).get("texto") or "").strip()
    if not item_id or not texto:
        raise HTTPException(status_code=422, detail="Informe item_id e texto.")
    return _ml_run(lambda ml: ml.atualizar_descricao(item_id, texto, user.id))


@app.post("/api/mercadolivre/anuncio-foto")
def ml_add_foto(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    item_id = (payload or {}).get("item_id")
    url = ((payload or {}).get("url") or "").strip()
    if not item_id or not url:
        raise HTTPException(status_code=422, detail="Informe item_id e a URL da imagem.")
    return _ml_run(lambda ml: ml.adicionar_foto(item_id, url, user.id))


@app.post("/api/mercadolivre/anuncio-ficha")
def ml_set_ficha(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    item_id = (payload or {}).get("item_id")
    ean = ((payload or {}).get("ean") or "").strip()
    peso = ((payload or {}).get("peso") or "").strip()
    attrs = []
    if ean:
        attrs.append({"id": "GTIN", "value_name": ean})
    if peso:
        attrs.append({"id": "WEIGHT", "value_name": peso})
    if not item_id or not attrs:
        raise HTTPException(status_code=422, detail="Informe item_id e ao menos EAN ou peso.")
    return _ml_run(lambda ml: ml.atualizar_atributos(item_id, attrs, user.id))


# --- Líquido / tarifas / frete ---
@app.get("/api/mercadolivre/tarifas")
def ml_tarifas(category_id: str = "", price: float = 0.0, listing_type_id: str = "gold_special",
               logistic_type: str = "", user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.tarifas_de_venda(category_id or None, price, listing_type_id,
                                                   logistic_type or None, user_id=user.id))


@app.get("/api/mercadolivre/liquido")
def ml_liquido(price: float, category_id: str = "", listing_type_id: str = "gold_special",
               logistic_type: str = "", frete: float = 0.0, imposto_pct: float = 0.0,
               custo: float = 0.0, user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.calcular_liquido(price, category_id or None, listing_type_id,
                   logistic_type or None, frete, imposto_pct, custo, user.id))


@app.get("/api/mercadolivre/frete")
def ml_frete(item_id: str, cep: str, user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.frete_do_item(item_id, cep, user.id))


@app.get("/api/mercadolivre/preco-venda/{item_id}")
def ml_preco_venda(item_id: str, user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.preco_de_venda(item_id, user.id))


# --- Radar de concorrência ---
@app.get("/api/mercadolivre/radar/{item_id}")
def ml_radar(item_id: str, user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.referencia_de_preco(item_id, user.id))


@app.get("/api/mercadolivre/radar-itens")
def ml_radar_itens(user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.itens_com_referencia(user_id=user.id))


@app.get("/api/mercadolivre/concorrentes")
def ml_concorrentes(q: str, category_id: str = "", user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.concorrentes(q, category_id or None, user_id=user.id))


# --- Pedidos ---
@app.get("/api/mercadolivre/pedidos")
def ml_pedidos(status: str = "paid", desde: str = "", ate: str = "", offset: int = 0,
               user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.listar_pedidos(user.id, status or None, desde or None,
                                                 ate or None, offset))


@app.get("/api/mercadolivre/pedido/{order_id}")
def ml_pedido(order_id: str, user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.obter_pedido(order_id, user.id))


def _parse_dt_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return None


_PAG_METODO = {
    "credit_card": "Cartão de crédito", "debit_card": "Cartão de débito",
    "account_money": "Saldo Mercado Pago", "ticket": "Boleto", "pix": "Pix",
    "bank_transfer": "Pix / Transferência", "digital_currency": "Cripto",
}
_SINAL_TAG = {
    "fraud_risk_detected": {"tipo": "fraude", "label": "Risco de fraude detectado", "tom": "danger"},
    "catalog": {"tipo": "catalogo", "label": "Venda de catálogo", "tom": "dim"},
    "test_order": {"tipo": "teste", "label": "Pedido de teste", "tom": "warn"},
}


def _balde_pedido(order_status, env, hoje_fim):
    """Classifica o pedido nos baldes do painel a partir do estado REAL do envio
    (mesmo eixo da tela Vendas do ML): a despachar hoje / próximos dias / aguardando
    NF-e / em trânsito / finalizado / cancelado. Sem envio no cache => 'sincronizando'."""
    if order_status == "cancelled":
        return "cancelado"
    if not env:
        return "sincronizando"
    st = env.get("status")
    sub = env.get("substatus") or ""
    if st == "cancelled":
        return "cancelado"
    if st == "delivered" or env.get("devolucao"):
        return "finalizado"
    if st == "shipped" or sub in ("in_hub", "in_transit", "out_for_delivery", "dropped_off", "picked_up", "receiver_absent"):
        return "transito"
    if env.get("fiscal_pendente"):
        return "fiscal"  # bloqueado até emitir NF-e — aba própria
    # ready/handling/buffered → hoje vs próximos pela data efetiva de coleta
    buffering = _parse_dt_iso(env.get("buffering_date"))
    if sub == "buffered" or buffering:
        return "hoje" if (buffering and buffering <= hoje_fim) else "proximos"
    hl = _parse_dt_iso(env.get("handling_limit"))
    if hl and hl > hoje_fim:
        return "proximos"
    return "hoje"


# ── Disciplina de tráfego ML: cache de janela (45s) + semáforo global ──
# Toda chamada do painel (1ª leva, Plano A, blocos do Plano B, poll) reutiliza a
# MESMA janela bruta de pedidos, estendendo-a incrementalmente. O semáforo impede
# que rajadas paralelas saturem a API do ML (rate limit era a causa dos minutos
# de espera: dezenas de buscas simultâneas -> 429 -> retries -> fila infinita).
import threading as _thr
import time as _time
_ML_SEM = _thr.BoundedSemaphore(6)
_JANELAS = {}
_JANELAS_LOCK = _thr.Lock()
_JANELA_TTL = 600.0      # a janela vive 10 min — jamais descartada no meio de uma carga
_JANELA_FRESCOR = 45.0   # após 45s, a página 0 é re-checada p/ pedidos novos (sem destruir nada)
_PCFG_CACHE = {}
_NFE_AQUECENDO = {}

_SYNC_PED = {}
_SYNC_PED_LOCK = _thr.Lock()


def _iso_para_dt(v):
    """ISO (com Z ou offset) -> datetime UTC naive, para comparação estável no banco."""
    import datetime as _dt
    try:
        d = _dt.datetime.fromisoformat(str(v).replace('Z', '+00:00'))
        if d.tzinfo is not None:
            d = d.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return d
    except Exception:  # noqa: BLE001
        return None


def _varrer_pedidos_ml(ml, user_id, desde, ate, alvo_max=600):
    """Varre o /orders/search em páginas de 50 e grava cada pedido no MLPedidoCache.
    Cadência deliberadamente folgada (0.35s entre páginas + backoff 429 do _req):
    a API nunca é saturada e o painel nunca depende dela ao vivo."""
    from .models import MLPedidoCache
    import datetime as _dt
    st = _SYNC_PED.setdefault(user_id, {})
    try:
        offset, total = 0, None
        while offset < (min(total, alvo_max) if total is not None else alvo_max):
            r = None
            for _tent in range(2):
                try:
                    with _ML_SEM:
                        r = ml.listar_pedidos(user_id, None, desde or None, ate or None, offset, 50) or {}
                    break
                except Exception:  # noqa: BLE001
                    _time.sleep(2.0)
            if r is None:
                break
            res = r.get('results') or []
            _pg_total = (r.get('paging') or {}).get('total')
            print(f"[varredura] user={user_id} offset={offset} recebidos={len(res)} total_ml={_pg_total} desde={desde} ate={ate}", flush=True)
            if total is None:
                total = int(_pg_total or len(res))
                st['total'] = min(total, alvo_max)
            if not res:
                print(f"[varredura] user={user_id} PAGINA VAZIA em offset={offset} — encerrando", flush=True)
                break
            db = SessionLocal()
            try:
                for o in res:
                    oid = str(o.get('id'))
                    row = db.query(MLPedidoCache).filter_by(user_id=user_id, order_id=oid).first()
                    if not row:
                        row = MLPedidoCache(user_id=user_id, order_id=oid)
                        db.add(row)
                    row.raw = o
                    row.status = str(o.get('status') or '')
                    row.pack_id = str(o.get('pack_id') or '') or None
                    row.total_amount = float(o.get('total_amount') or 0)
                    row.paid_amount = float(o.get('paid_amount') or 0)
                    row.currency_id = str(o.get('currency_id') or '') or None
                    _its = o.get('order_items') or []
                    row.unidades = sum(int(i.get('quantity') or 0) for i in _its)
                    row.itens = [{'item_id': (i.get('item') or {}).get('id'), 'sku': (i.get('item') or {}).get('seller_sku'),
                                  'titulo': (i.get('item') or {}).get('title'), 'quantidade': i.get('quantity'),
                                  'unit_price': i.get('unit_price'), 'sale_fee': i.get('sale_fee')} for i in _its]
                    dcv = _iso_para_dt(o.get('date_created'))
                    if dcv:
                        row.date_created = dcv
                    dfc = _iso_para_dt(o.get('date_closed'))
                    if dfc:
                        row.date_closed = dfc
                    row.atualizado_em = _dt.datetime.utcnow()
                db.commit()
            finally:
                db.close()
            offset += len(res)
            st['progresso'] = offset
            _time.sleep(0.35)  # folga: muito abaixo do limite da API do ML
    finally:
        st['rodando'] = False
        st['ts'] = _time.time()
        try:
            from .models import MLPedidoCache as _MP
            _db = SessionLocal()
            _n = _db.query(_MP).filter(_MP.user_id == user_id).count()
            _db.close()
            print(f"[varredura] user={user_id} FIM · gravados_total_no_banco={_n} · progresso={st.get('progresso')}", flush=True)
        except Exception as _e:  # noqa: BLE001
            print(f"[varredura] user={user_id} FIM (erro ao contar: {_e})", flush=True)


def _garantir_sync_pedidos(ml, user_id, desde, ate):
    """Dispara UMA varredura de fundo se o banco estiver dessincronizado há 60s+."""
    st = _SYNC_PED.setdefault(user_id, {})
    with _SYNC_PED_LOCK:
        if st.get('rodando'):
            return st
        if _time.time() - (st.get('ts') or 0) < 60:
            return st
        st['rodando'] = True
        st['progresso'] = 0
    _thr.Thread(target=_varrer_pedidos_ml, args=(ml, user_id, desde, ate), daemon=True).start()
    return st


def _janela_ml(ml, user_id, status, desde, ate, precisa_ate):
    """Garante uma janela bruta de pedidos [0..precisa_ate) fresca, estendendo a
    existente em blocos de 50 paralelos (máx 5 por vez, sob semáforo)."""
    chave = (user_id, status or '', desde or '', ate or '')
    with _JANELAS_LOCK:
        j = _JANELAS.get(chave)
        if j and (_time.time() - j['ts'] > _JANELA_TTL):
            j = None
        if j is None:
            j = {'ts': _time.time(), 'results': [], 'total': None, 'fim': False, 'lock': _thr.Lock()}
            _JANELAS[chave] = j
    with j['lock']:  # uma extensão por vez; as demais chamadas esperam e reutilizam
        # frescor: pedidos novos entram pelo topo — re-checa a página 0 sem descartar o acumulado
        if j['results'] and (_time.time() - j['ts'] > _JANELA_FRESCOR):
            try:
                with _ML_SEM:
                    r0 = ml.listar_pedidos(user_id, status or None, desde or None, ate or None, 0, 50) or {}
                tot0 = (r0.get('paging') or {}).get('total')
                if tot0 is not None:
                    j['total'] = int(tot0)
                vistos = {str(o.get('id')) for o in j['results']}
                frescos = [o for o in (r0.get('results') or []) if str(o.get('id')) not in vistos]
                if frescos:
                    j['results'] = frescos + j['results']
            except Exception:  # noqa: BLE001
                pass
            j['ts'] = _time.time()
        if j['total'] is not None:
            precisa_ate = min(precisa_ate, j['total'])
        while len(j['results']) < precisa_ate and not j['fim']:
            base = len(j['results'])
            falta = precisa_ate - base
            offsets = list(range(base, base + falta, 50))[:5]
            def _pg(off):
                for _ in range(2):
                    try:
                        with _ML_SEM:
                            r = ml.listar_pedidos(user_id, status or None, desde or None, ate or None,
                                                  off, min(50, precisa_ate - off)) or {}
                        return off, (r.get('results') or []), (r.get('paging') or {}).get('total')
                    except Exception:  # noqa: BLE001
                        _time.sleep(0.6)
                return off, [], None
            if len(offsets) == 1:
                paginas = [_pg(offsets[0])]
            else:
                from concurrent.futures import ThreadPoolExecutor as _TPE
                with _TPE(max_workers=len(offsets)) as _ex:
                    paginas = list(_ex.map(_pg, offsets))
            avancou = False
            for _off, res, tot in sorted(paginas, key=lambda x: x[0]):
                if tot is not None and j['total'] is None:
                    j['total'] = int(tot)
                    precisa_ate = min(precisa_ate, j['total'])
                if _off == len(j['results']) and res:
                    j['results'].extend(res)
                    avancou = True
                elif _off == len(j['results']) and not res:
                    j['fim'] = True
            if not avancou:
                break
        j['ts'] = _time.time()
        total = j['total'] if j['total'] is not None else len(j['results'])
        return list(j['results']), total


def _pedidos_ml_enriquecidos(ml, user_id, status, offset, limit, desde=None, ate=None):
    """Pedidos do ML cruzados com o Bling (preço/custo por SKU) e com o cache do
    anúncio (imagem/preço atual). A tarifa vem do próprio pedido (order_items.sale_fee),
    então não há chamada extra à API. Devolve pedidos + estatísticas agregadas."""
    from .models import ProdutoCache, MLItemCache, MLEnvioCache
    from sqlalchemy import or_ as _or

    # O ML corta /orders/search em 50 por chamada. Para o modelo de "janela inteira"
    # (o front pede a janela toda de uma vez, ex.: limit=120), buscamos em blocos de 50
    # até completar `limit` (teto de segurança 150). `total` vem do paging da 1ª página.
    _t0 = _time.time()
    limit = max(1, min(int(limit or 30), 500))
    offset = max(0, int(offset or 0))
    # O painel lê do BANCO em milissegundos; a varredura de fundo mantém o banco vivo.
    _sync_st = _garantir_sync_pedidos(ml, user_id, desde, ate)   # nome próprio: o laço abaixo usa `st` p/ status do pedido
    dbp = SessionLocal()
    try:
        from .models import MLPedidoCache as _MPC
        q = dbp.query(_MPC).filter(_MPC.user_id == user_id, _MPC.raw.isnot(None))
        _dd = _iso_para_dt(desde) if desde else None
        _da = _iso_para_dt(ate) if ate else None
        if _dd is not None:
            q = q.filter(_MPC.date_created >= _dd)
        if _da is not None:
            q = q.filter(_MPC.date_created <= _da)
        if status:
            q = q.filter(_MPC.status == status)
        q = q.order_by(_MPC.date_created.desc())
        total = q.count()
        results = [r.raw for r in q.offset(offset).limit(limit).all() if r.raw]  # payload vazio: fora
    finally:
        dbp.close()
    paging = {"total": total, "carregados": len(results),
              "sync": {"rodando": bool(_sync_st.get('rodando')), "progresso": int(_sync_st.get('progresso') or 0),
                       "alvo": int(_sync_st.get('total') or 0), "ultima": _sync_st.get('ts') or 0}}
    if _time.time() - _t0 > 5:
        print(f"[pedidos_ml] lento: {_time.time()-_t0:.1f}s offset={offset} limit={limit} lidos={len(results)} total={total}", flush=True)

    # ENVIOS: o /orders/search não devolve mais o estado do shipment — o cache local
    # (webhooks + backfill) é a fonte de verdade de status/prazo/rastreio/destinatário.
    envio_cache = {}
    try:
        _sids = []
        for o in results:
            _sid = (o.get("shipping") or {}).get("id")
            if _sid:
                _sids.append(str(_sid))
        if _sids:
            _dbe = SessionLocal()
            try:
                for i in range(0, len(_sids), 400):
                    for e in _dbe.query(MLEnvioCache).filter(
                        MLEnvioCache.user_id == user_id,
                        MLEnvioCache.shipment_id.in_(_sids[i:i + 400]),
                    ).all():
                        envio_cache[str(e.shipment_id)] = e
            finally:
                _dbe.close()
    except Exception:  # noqa: BLE001
        envio_cache = {}

    skus, item_ids = set(), set()
    for o in results:
        for it in (o.get("order_items") or []):
            item = it.get("item") or {}
            if item.get("seller_sku"):
                skus.add(str(item["seller_sku"]))
            if item.get("id"):
                item_ids.add(str(item["id"]))

    prod_by_sku, ml_by_item, ml_by_sku = {}, {}, {}
    db = SessionLocal()
    try:
        if skus:
            for r in db.query(ProdutoCache).filter(
                    ProdutoCache.user_id == user_id, ProdutoCache.sku.in_(list(skus))).all():
                if r.sku:
                    prod_by_sku[str(r.sku)] = r
        conds = []
        if item_ids:
            conds.append(MLItemCache.item_id.in_(list(item_ids)))
        if skus:
            conds.append(MLItemCache.sku.in_(list(skus)))
        if conds:
            for r in db.query(MLItemCache).filter(
                    MLItemCache.user_id == user_id, _or(*conds)).all():
                if r.item_id:
                    ml_by_item[str(r.item_id)] = r
                if r.sku:
                    ml_by_sku[str(r.sku)] = r
    finally:
        db.close()

    pedidos = []
    t_rec = t_fee = t_cost = t_liq = 0.0
    t_unid = 0
    _pc = _PCFG_CACHE.get(user_id)
    if _pc and (_time.time() - _pc[0] < 60):
        _pcfg = _pc[1]
    else:
        _pcfg = precificacao.obter_config(user_id)  # taxas/faixas da tela de Configurações
        _PCFG_CACHE[user_id] = (_time.time(), _pcfg)
    por_status, por_dia = {}, {}
    for o in results:
        itens = []
        o_rec = o_fee = o_bling = 0.0
        o_taxas_cfg = o_liq_cfg = 0.0
        o_unid = 0
        o_logistic = None
        for it in (o.get("order_items") or []):
            item = it.get("item") or {}
            iid = str(item.get("id") or "")
            sku = str(item.get("seller_sku") or "")
            qty = int(it.get("quantity") or 0)
            unit = float(it.get("unit_price") or 0)
            fee_u = float(it.get("sale_fee") or 0)
            prod = prod_by_sku.get(sku)
            mlc = ml_by_item.get(iid) or ml_by_sku.get(sku)
            if not o_logistic and mlc is not None:
                o_logistic = getattr(mlc, "logistic_type", None)
            preco_bling = float(prod.preco) if (prod and prod.preco) else None  # alvo (deve "sobrar" isso)
            custo_prod = float(prod.custo) if (prod and prod.custo) else None  # precoCusto do Bling
            ml_preco = float(mlc.preco) if (mlc and mlc.preco) else None
            imagem = (mlc.imagem if (mlc and mlc.imagem) else (prod.imagem if (prod and prod.imagem) else None))
            titulo = item.get("title") or (prod.nome if prod else None) or (mlc.titulo if mlc else None) or "Item"
            rev = unit * qty
            fee = fee_u * qty
            mr = precificacao.margem_real_canal(_pcfg, "mercadolivre", unit, custo_prod) or {}
            liq_u = mr.get("liquido")
            liq_cfg_item = (liq_u * qty) if liq_u is not None else (rev - fee)
            liq_item = rev - fee   # REAL: preço pago − tarifa que o ML cobrou (sale_fee)
            itens.append({
                "item_id": iid or None, "sku": sku or None, "titulo": titulo,
                "imagem": imagem, "quantidade": qty, "unit_price": round(unit, 2),
                "sale_fee": round(fee, 2), "preco_bling": preco_bling, "custo": custo_prod,
                "ml_preco": ml_preco, "receita": round(rev, 2),
                "liquido": round(liq_item, 2),          # real (receita − sale_fee)
                "taxas_mkt": round(fee, 2),             # o que o ML cobrou de fato
                "taxas_cfg": round((mr.get("taxas") or 0) * qty, 2) or None,   # estimativa da tabela
                "liquido_cfg": round(liq_cfg_item, 2),                          # líquido estimado
                "lucro": round(liq_item - (custo_prod or 0) * qty, 2) if custo_prod else None,
                "margem": round(liq_item / rev * 100, 1) if rev > 0 else None,
            })
            o_rec += rev; o_fee += fee; o_bling += (preco_bling or 0) * qty; o_unid += qty
            o_taxas_cfg += (mr.get("taxas") or 0) * qty
            o_liq_cfg += liq_cfg_item
        o_liq = o_rec - o_fee   # REAL (o frete entra depois, quando o cache de envios é lido)
        st = o.get("status")
        por_status[st] = por_status.get(st, 0) + 1
        dia = (o.get("date_created") or "")[:10]
        if dia:
            por_dia[dia] = por_dia.get(dia, 0.0) + o_rec
        buyer = o.get("buyer") or {}
        ship = o.get("shipping") or {}
        logistic = ship.get("logistic_type") or o_logistic
        pago_em = next((pg.get("date_approved") for pg in (o.get("payments") or [])
                        if pg.get("date_approved")), None)
        _pgs = o.get("payments") or []
        _pg0 = _pgs[0] if _pgs else {}
        pagamento = None
        if _pg0:
            _parc = _pg0.get("installments")
            pagamento = {
                "metodo": _PAG_METODO.get(_pg0.get("payment_method_id")) or _PAG_METODO.get(_pg0.get("payment_type")) or _pg0.get("payment_type"),
                "parcelas": _parc if _parc and _parc > 1 else None,
                "valor_parcela": round(float(_pg0.get("installment_amount")), 2) if _pg0.get("installment_amount") else None,
                "aprovado_em": _pg0.get("date_approved"),
            }
        sinais = [dict(_SINAL_TAG[t]) for t in (o.get("tags") or []) if t in _SINAL_TAG]
        if o.get("mediations"):
            sinais.append({"tipo": "mediacao", "label": "Mediação aberta", "tom": "warn"})
        pedidos.append({
            "id": o.get("id"),
            "pack_id": o.get("pack_id"),
            "date_created": o.get("date_created"),
            "pago_em": pago_em,
            "pagamento": pagamento,
            "sinais": sinais,
            "status": st,
            "buyer": {"nickname": buyer.get("nickname"), "id": buyer.get("id")},
            "shipping_id": ship.get("id"),
            "envio_status": (getattr(envio_cache.get(str(ship.get("id") or "")), "status", None)
                             or ship.get("status")),
            "envio_substatus": getattr(envio_cache.get(str(ship.get("id") or "")), "substatus", None),
            "ship_by": (lambda _e: (_e.handling_limit.timestamp() if _e is not None and _e.handling_limit else None))(envio_cache.get(str(ship.get("id") or ""))),
            "rastreio": getattr(envio_cache.get(str(ship.get("id") or "")), "tracking_number", None),
            "uf": getattr(envio_cache.get(str(ship.get("id") or "")), "receiver_estado", None),
            "cidade": getattr(envio_cache.get(str(ship.get("id") or "")), "receiver_cidade", None),
            "cep": getattr(envio_cache.get(str(ship.get("id") or "")), "receiver_cep", None),
            "cliente": getattr(envio_cache.get(str(ship.get("id") or "")), "receiver_nome", None),
            "endereco": getattr(envio_cache.get(str(ship.get("id") or "")), "receiver_endereco", None),
            "frete_vendedor": getattr(envio_cache.get(str(ship.get("id") or "")), "custo_vendedor", None),
            "devolucao_envio": bool(getattr(envio_cache.get(str(ship.get("id") or "")), "devolucao", False)),
            "tags": o.get("tags") or [],
            "logistic_type": logistic,
            "is_full": (logistic == "fulfillment"),
            "total": float(o.get("total_amount") or o_rec),
            "itens": itens,
            "resumo": {
                "receita": round(o_rec, 2),
                "tarifa": round(o_fee, 2),  # comissão REAL cobrada pelo ML (sale_fee)
                "taxas_cfg": round(o_taxas_cfg, 2),  # ESTIMATIVA da sua tabela (faixa+imposto+cartão) — comparação, não cobrança
                "liquido_cfg": round(o_liq_cfg, 2),  # líquido pela estimativa (planejamento)
                "preco_bling": round(o_bling, 2) if o_bling else None,  # alvo: quanto deve sobrar
                # CONTA REAL (igual ao extrato do ML): receita − sale_fee − frete do vendedor.
                # O frete entra logo abaixo, quando o cache de envios é lido.
                "liquido": round(o_rec - o_fee, 2), "unidades": o_unid, "estornado": (st == "cancelled"),
                "margem": round((o_rec - o_fee) / o_rec * 100, 1) if o_rec > 0 else None,
            },
        })
        t_rec += o_rec; t_fee += o_fee; t_cost += o_taxas_cfg; t_liq += o_liq; t_unid += o_unid

    # --- Estado real do envio: lê do cache (hot-path leve). Webhooks do tópico
    #     `shipments` + backfill sob demanda mantêm o cache vivo. ---
    sids = [str(p["shipping_id"]) for p in pedidos if p.get("shipping_id")]
    envios_cache = {}
    if sids:
        _db2 = SessionLocal()
        try:
            envios_cache = ml.ler_envios_cache(_db2, user_id, sids)
        finally:
            _db2.close()
    baldes = {"hoje": 0, "proximos": 0, "fiscal": 0, "transito": 0, "finalizado": 0, "cancelado": 0, "sincronizando": 0}
    n_fiscal = n_devol = n_sync = 0
    t_frete = 0.0
    hoje_fim = datetime.utcnow().replace(hour=23, minute=59, second=59, microsecond=0)
    # NF-e do Bling: pedidos que o ML marca como "aguardando NF-e" mas que JÁ TÊM nota
    # autorizada no Bling saem de "Aguardando NF-e" (o status do pedido muda). Limitado
    # aos candidatos fiscais e cacheado — não pesa no hot-path.
    nfe_ok_ids = set()
    fiscal_cand = [str(p.get("id")) for p in pedidos
                   if (envios_cache.get(str(p.get("shipping_id"))) or {}).get("fiscal_pendente")]
    if fiscal_cand:
        # HOT-PATH: NUNCA chamar o Bling aqui. `nfe_por_pedidos` varre até 8 páginas + 120
        # detalhes (dezenas de HTTP) e travava a resposta do painel quando havia muitos
        # candidatos fiscais. Aqui só LEMOS o cache; o que faltar é aquecido em FUNDO e
        # aparece na próxima leitura (o poll de 60s do painel já traz).
        try:
            from . import nfe as _nfe_mod
            _ent = _nfe_mod._NFE_POR_PEDIDO_CACHE.get(user_id)
            _cache_nfe = dict(_ent[1]) if (_ent and _time.time() - _ent[0] < _nfe_mod._NFE_POR_PEDIDO_TTL) else {}
            _alvo = set(fiscal_cand)
            for _oid, _info in _cache_nfe.items():
                if str(_oid) in _alvo and _nfe_mod.nota_autorizada((_info or {}).get("nfe_situacao")):
                    nfe_ok_ids.add(str(_oid))
            _faltam = [x for x in fiscal_cand if x not in _cache_nfe]
            if _faltam and not _NFE_AQUECENDO.get(user_id):
                _NFE_AQUECENDO[user_id] = True

                def _aquecer_nfe(_uid=user_id, _ids=list(_faltam)):
                    try:
                        _nfe_mod.nfe_por_pedidos(_uid, _ids)
                        print(f"[nfe] cache aquecido em fundo · user={_uid} pedidos={len(_ids)}", flush=True)
                    except Exception as _e:  # noqa: BLE001
                        print(f"[nfe] aquecimento falhou: {_e}", flush=True)
                    finally:
                        _NFE_AQUECENDO.pop(_uid, None)

                _thr.Thread(target=_aquecer_nfe, daemon=True).start()
        except Exception:  # noqa: BLE001
            pass
    for p in pedidos:
        env = envios_cache.get(str(p.get("shipping_id"))) if p.get("shipping_id") else None
        if env and str(p.get("id")) in nfe_ok_ids and env.get("fiscal_pendente"):
            env = dict(env)
            env["fiscal_pendente"] = False
            env["nfe_emitida"] = True  # nota autorizada no Bling → liberado p/ despacho
        p["envio"] = env
        balde = _balde_pedido(p.get("status"), env, hoje_fim)
        p["balde"] = balde
        baldes[balde] = baldes.get(balde, 0) + 1
        r = p.get("resumo") or {}
        frete = float((env or {}).get("custo_vendedor") or 0) if env else 0.0
        # CONTA REAL DO CANAL (espelha o extrato do ML):
        #   receita − tarifa de venda (sale_fee) − frete por conta do vendedor = líquido
        # A estimativa da tabela de preços fica em taxas_cfg/liquido_cfg, para comparação.
        r["frete_vendedor"] = round(frete, 2)
        r["tarifa_total"] = round((r.get("tarifa") or 0) + frete, 2)
        r["comissao_pct"] = round((r.get("tarifa") or 0) / r["receita"] * 100, 1) if r.get("receita") else None
        r["liquido"] = round((r.get("receita") or 0) - (r.get("tarifa") or 0) - frete, 2)
        r["margem"] = round(r["liquido"] / r["receita"] * 100, 1) if r.get("receita") else None
        p["resumo"] = r
        t_frete += frete
        if env:
            if env.get("status"):
                p["envio_status"] = env.get("status")
            if env.get("fiscal_pendente"):
                n_fiscal += 1
            if env.get("devolucao"):
                n_devol += 1
        else:
            n_sync += 1

    n = len(results)
    liq_final = t_liq - t_frete
    stats = {
        "pedidos": n, "receita": round(t_rec, 2),
        "ticket_medio": round(t_rec / n, 2) if n else 0,
        "unidades": t_unid, "tarifas": round(t_fee, 2),
        "frete_vendedor": round(t_frete, 2),
        "custos_ml": round(t_fee + t_frete, 2),
        "custo": round(t_cost, 2), "impostos": round(t_cost, 2), "liquido": round(liq_final, 2),
        "margem": round(liq_final / t_rec * 100, 1) if t_rec > 0 else None,
        "por_status": por_status,
        "baldes": baldes,
        "fiscal_pendentes": n_fiscal,
        "devolucoes": n_devol,
        "sincronizando": n_sync,
        "receita_por_dia": [{"dia": k, "receita": round(v, 2)} for k, v in sorted(por_dia.items())],
    }
    _dur = _time.time() - _t0
    if _dur > 3:
        print(f"[pedidos_ml] enriquecimento {_dur:.1f}s · {len(pedidos)} pedidos · envios={len(envio_cache)} skus={len(skus)} · sync_rodando={bool(_sync_st.get('rodando'))}", flush=True)
    return {"pedidos": pedidos, "paging": paging, "stats": stats}


@app.get("/api/mercadolivre/_diag_pedidos")
def diag_pedidos_ml(dias: int = 15, user: User = Depends(auth.get_current_user)):
    """Diagnóstico completo: ML real -> banco -> endpoint do painel. Sem depender de logs."""
    import datetime as _dt
    desde = (_dt.datetime.utcnow() - _dt.timedelta(days=dias)).replace(hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%S.000-00:00')
    passos = []

    def _run(ml):
        # 1) conta ML
        try:
            passos.append(f"seller_id={ml._seller_id(user.id)}")
        except Exception as e:  # noqa: BLE001
            passos.append(f"seller_id ERRO: {e}")
        # 2) ML real
        try:
            with _ML_SEM:
                r = ml.listar_pedidos(user.id, None, desde, None, 0, 50) or {}
            res = r.get("results") or []
            passos.append(f"orders/search: recebidos={len(res)} total_ml={(r.get('paging') or {}).get('total')}")
            if res:
                passos.append(f"amostra ML: id={res[0].get('id')} status={res[0].get('status')} date_created={res[0].get('date_created')}")
        except Exception as e:  # noqa: BLE001
            passos.append(f"orders/search ERRO: {type(e).__name__}: {str(e)[:200]}")
        # 3) banco
        try:
            from .models import MLPedidoCache as _MP
            _db = SessionLocal()
            _dd = _iso_para_dt(desde)
            n_total = _db.query(_MP).filter(_MP.user_id == user.id).count()
            n_raw = _db.query(_MP).filter(_MP.user_id == user.id, _MP.raw.isnot(None)).count()
            _qp = _db.query(_MP).filter(_MP.user_id == user.id, _MP.raw.isnot(None), _MP.date_created >= _dd)
            n_painel = _qp.count()
            am = _qp.first()
            passos.append(f"banco: total={n_total} com_raw={n_raw} · FILTRO DO PAINEL retornaria={n_painel}")
            if am:
                passos.append(f"banco amostra: order_id={am.order_id} date_created={am.date_created} raw_valido={bool(am.raw)}")
            _db.close()
        except Exception as e:  # noqa: BLE001
            passos.append(f"banco ERRO: {type(e).__name__}: {str(e)[:200]}")
        # 4) TESTE DEFINITIVO: o caminho EXATO do painel, com enriquecimento
        for _lim in (25,):
            try:
                _t = _time.time()
                _rr = _pedidos_ml_enriquecidos(ml, user.id, '', 0, _lim, desde, None)
                _peds = _rr.get('pedidos') or []
                passos.append(f"ENDPOINT DO PAINEL limit={_lim}: {len(_peds)} pedidos em {_time.time()-_t:.1f}s · paging={_rr.get('paging', {}).get('total')}")
                if _peds:
                    _p = _peds[0]
                    passos.append(f"amostra enriquecida: id={_p.get('id')} envio_status={_p.get('envio_status')} uf={_p.get('uf')} nfe={_p.get('nfe')} shipping_id={_p.get('shipping_id')}")
            except Exception as e:  # noqa: BLE001
                import traceback as _tb
                passos.append(f"ENDPOINT DO PAINEL limit={_lim} ERRO: {type(e).__name__}: {str(e)[:250]}")
                passos.append(f"trace: {_tb.format_exc()[-350:]}")
        return {"desde": desde, "passos": passos}

    return _ml_run(_run)


@app.get("/api/mercadolivre/_diag_nfe")
def diag_nfe_ml(user: User = Depends(auth.get_current_user)):
    """Mostra o que o Bling grava em `pedido_loja` nas últimas notas e se casa com os
    order_ids do ML. É aqui que se descobre por que a NF-e não aparece no painel."""
    from . import bling, nfe as _nfe
    from datetime import date, timedelta
    out = {"amostra_notas_bling": [], "amostra_pedidos_ml": [], "diagnostico": ""}
    # 1) últimos pedidos ML do banco
    try:
        from .models import MLPedidoCache as _MP
        _db = SessionLocal()
        rows = _db.query(_MP).filter(_MP.user_id == user.id, _MP.raw.isnot(None)).order_by(_MP.date_created.desc()).limit(8).all()
        ml_ids, ml_packs = [], []
        for r in rows:
            ml_ids.append(str(r.order_id))
            if (r.raw or {}).get("pack_id"):
                ml_packs.append(str((r.raw or {}).get("pack_id")))
        out["amostra_pedidos_ml"] = [{"order_id": i} for i in ml_ids]
        out["packs_ml"] = ml_packs[:8]
        _db.close()
    except Exception as e:  # noqa: BLE001
        out["erro_banco"] = str(e)[:200]
        ml_ids = []
    # 2) últimas notas do Bling com o campo de casamento
    try:
        fim = date.today(); ini = fim - timedelta(days=30)
        raw = bling.listar_nfe(user.id, pagina=1, limite=20, data_ini=ini.isoformat(), data_fim=fim.isoformat())
        linhas = _nfe.resumir_lista(raw) or []
        out["notas_encontradas_bling"] = len(linhas)
        for n in linhas[:8]:
            nid = n.get("id")
            item = {"id": nid, "numero": n.get("numero"), "tipo": n.get("tipo")}
            try:
                det = _nfe.detalhar_nfe(bling.obter_nfe(user.id, nid))
                item["pedido_loja"] = det.get("pedido_loja")
                item["numero_nf"] = det.get("numero")
                item["situacao"] = det.get("situacao_label") or det.get("situacao")
                item["destinatario"] = (det.get("destinatario") or {}).get("nome")
            except Exception as e:  # noqa: BLE001
                item["erro_detalhe"] = str(e)[:120]
            out["amostra_notas_bling"].append(item)
    except Exception as e:  # noqa: BLE001
        out["erro_bling"] = f"{type(e).__name__}: {str(e)[:200]}"
    # 3) veredito: os pedido_loja casam com os order_ids do ML?
    try:
        pl = {str(x.get("pedido_loja") or "") for x in out["amostra_notas_bling"] if x.get("pedido_loja")}
        casam = pl & set(ml_ids)
        out["diagnostico"] = (f"pedido_loja das notas: {sorted(pl)[:5]} · order_ids ML: {ml_ids[:5]} · "
                              f"CASAM={sorted(casam)[:5] if casam else 'NENHUM — formato diferente!'}")
    except Exception as e:  # noqa: BLE001
        out["diagnostico"] = f"erro: {e}"
    return out


@app.post("/api/shopee/comprador")
def shopee_comprador_ep(payload: dict = Body(default={}), user: User = Depends(auth.get_current_user)):
    """Dados do comprador Shopee SEM máscara (READY_TO_SHIP / PROCESSED / TO_RETURN).
    Usa `response_optional_fields` com recipient_address, phone, buyer_cpf_id e buyer_username."""
    from . import shopee_comprador as _sc
    sns = [str(x) for x in (payload.get("order_sns") or []) if x][:200]
    if not sns:
        return {"mapa": {}}
    try:
        return {"mapa": _sc.comprador(user.id, sns)}
    except Exception as e:  # noqa: BLE001
        return {"mapa": {}, "erro": f"{type(e).__name__}: {str(e)[:200]}"}


@app.get("/api/shopee/_diag_comprador")
def diag_comprador_shopee(user: User = Depends(auth.get_current_user)):
    """Diagnóstico: pega pedidos A_ENVIAR e mostra o que a Shopee devolve desmascarado."""
    from . import shopee as _sh, shopee_comprador as _sc
    out = {"passos": []}
    try:
        pn = _sh.pedidos_painel(user.id, "A_ENVIAR", 15, page=1, page_size=5) or {}
        peds = pn.get("pedidos") or []
        sns = [str(p.get("order_sn") or p.get("id")) for p in peds][:5]
        out["passos"].append(f"pedidos A_ENVIAR: {len(peds)} · sns={sns}")
        if peds:
            p0 = peds[0]
            out["passos"].append(f"ANTES (shopee.py): cliente={p0.get('cliente')} cidade={p0.get('cidade')} endereco={str(p0.get('endereco'))[:120]}")
        m = _sc.comprador(user.id, sns, forcar=True)
        for sn, v in list(m.items())[:3]:
            out["passos"].append(f"DEPOIS {sn}: desmascarado={v.get('desmascarado')} cliente={v.get('cliente')} cpf={v.get('cpf')} tel={v.get('telefone')} end={str((v.get('endereco') or {}).get('completo'))[:90]}")
    except Exception as e:  # noqa: BLE001
        import traceback
        out["passos"].append(f"ERRO: {type(e).__name__}: {str(e)[:200]}")
        out["trace"] = traceback.format_exc()[-350:]
    return out


@app.post("/api/nfe/gerar")
def nfe_gerar_ep(payload: dict = Body(default={}), user: User = Depends(auth.get_current_user)):
    """Gera NF-e (situação Pendente) a partir do pedido, via POST /nfe do Bling.
    SEGURANÇA: dry_run=True por padrão (só monta e devolve o corpo, NÃO gera).
    Passe dry_run=False para gerar de fato UMA nota. NÃO transmite ao Sefaz."""
    from . import nfe_gerar
    pedido = payload.get("pedido") or {}
    if not pedido:
        return {"ok": False, "erro": "envie 'pedido' com cliente e itens."}
    dry = payload.get("dry_run", True)
    # ENRIQUECIMENTO DO DESTINATÁRIO (servidor): o billing_info do ML traz o CPF mas nem
    # sempre o endereço. Completamos com o endereço de ENTREGA do envio, que é o que vai
    # na NF-e. Sem isto a nota sai com destinatário incompleto e o Sefaz rejeita.
    try:
        cli = pedido.setdefault("cliente", {})
        end = cli.setdefault("endereco", {})
        oid = str(pedido.get("numero_loja") or "")
        if oid and not (end.get("cidade") and end.get("completo")):
            from . import mercadolivre as ml
            if not cli.get("cpf_cnpj") or not end.get("cidade"):
                try:
                    df = ml.dados_fiscais_comprador(oid, user.id) or {}
                    if df.get("ok"):
                        cli["nome"] = cli.get("nome") or df.get("nome")
                        cli["cpf_cnpj"] = cli.get("cpf_cnpj") or df.get("doc_numero")
                        for k_src, k_dst in (("endereco", "completo"), ("bairro", "bairro"),
                                             ("cidade", "cidade"), ("estado", "uf"), ("cep", "cep")):
                            if df.get(k_src) and not end.get(k_dst):
                                end[k_dst] = df[k_src]
                except Exception:  # noqa: BLE001
                    pass
            # ainda sem cidade? usa o endereço de ENTREGA do cache de envios
            if not end.get("cidade"):
                try:
                    from .models import MLPedidoCache as _MP, MLEnvioCache as _ME
                    _db = SessionLocal()
                    row = _db.query(_MP).filter(_MP.user_id == user.id, _MP.order_id == oid).first()
                    ship_id = None
                    if row is not None and (row.raw or {}):
                        ship_id = str(((row.raw or {}).get("shipping") or {}).get("id") or "")
                    env = (_db.query(_ME).filter(_ME.user_id == user.id, _ME.shipment_id == ship_id).first()
                           if ship_id else None)
                    if env is not None:
                        end["completo"] = end.get("completo") or env.receiver_endereco
                        end["cidade"] = end.get("cidade") or env.receiver_cidade
                        end["uf"] = end.get("uf") or env.receiver_estado
                        end["cep"] = end.get("cep") or env.receiver_cep
                        cli["nome"] = cli.get("nome") or env.receiver_nome
                    _db.close()
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    try:
        return nfe_gerar.gerar(user.id, pedido, dry_run=bool(dry))
    except Exception as e:  # noqa: BLE001
        import traceback
        return {"ok": False, "erro": f"{type(e).__name__}: {str(e)[:200]}", "trace": traceback.format_exc()[-300:]}


@app.get("/api/nfe/_molde")
def nfe_molde_diag(user: User = Depends(auth.get_current_user)):
    """Diagnóstico: mostra a nota-molde que será usada de base (a mais recente do cliente)."""
    from . import nfe_gerar
    m = nfe_gerar._molde(user.id)
    if not m:
        return {"tem_molde": False, "aviso": "Nenhuma nota encontrada nos últimos 90 dias. Gere 1 no Bling para servir de molde."}
    return {"tem_molde": True, "id": m.get("id"), "numero": m.get("numero"),
            "natureza": m.get("naturezaOperacao"), "loja": m.get("loja"),
            "serie": m.get("serie"), "tipo": m.get("tipo"), "finalidade": m.get("finalidade"),
            "campos_disponiveis": sorted(list(m.keys()))}


@app.post("/api/mercadolivre/dados-fiscais")
def ml_dados_fiscais_ep(payload: dict = Body(default={}), user: User = Depends(auth.get_current_user)):
    """Nome + CPF/CNPJ + endereço fiscal do comprador ML (/orders/{id}/billing_info x-version:2).
    É o que faltava para gerar a NF-e — o CPF do ML não vem no pedido, só neste recurso."""
    from . import mercadolivre as ml
    import threading as _t
    ids = [str(x) for x in (payload.get("order_ids") or []) if x][:60]
    if not ids:
        return {"mapa": {}}
    mapa, lock = {}, _t.Lock()
    def _um(oid):
        try:
            d = ml.dados_fiscais_comprador(oid, user.id)
            # se veio ok mas sem doc, marca o porquê (Full? comprador sem CPF cadastrado?)
            if d.get("ok") and not d.get("doc_numero"):
                d["motivo_sem_doc"] = "billing_info retornou sem identification.number (comprador PF sem CPF no ML, ou pedido Full)"
        except Exception as e:  # noqa: BLE001
            d = {"ok": False, "erro": f"{type(e).__name__}: {str(e)[:150]}"}
        with lock:
            mapa[oid] = d
    ths = []
    for oid in ids:
        th = _t.Thread(target=_um, args=(oid,), daemon=True); th.start(); ths.append(th)
        if len(ths) >= 6:
            for t in ths: t.join(timeout=15)
            ths = []
    for t in ths: t.join(timeout=15)
    return {"mapa": mapa}


@app.get("/api/nfe/_diag_gerar")
def diag_gerar_nfe(order_id: str = "", user: User = Depends(auth.get_current_user)):
    """Roda o fluxo COMPLETO de geração para 1 pedido, mostrando cada etapa.
    Sem order_id, escolhe sozinho um pedido do cache que ainda não tem nota."""
    from . import mercadolivre as ml, nfe_gerar
    out = {"passos": []}
    try:
        # 1) escolher o pedido
        if not order_id:
            from .models import MLPedidoCache as _MP
            _db = SessionLocal()
            row = (_db.query(_MP).filter(_MP.user_id == user.id, _MP.raw.isnot(None))
                   .order_by(_MP.date_created.desc()).first())
            _db.close()
            if row is None:
                out["passos"].append("nenhum pedido no cache — carregue o painel primeiro")
                return out
            order_id = str(row.order_id)
            raw = row.raw or {}
        else:
            raw = ml.pedido(order_id, user.id) if hasattr(ml, "pedido") else {}
        out["order_id"] = order_id
        out["passos"].append(f"1) pedido escolhido: {order_id}")

        # 2) itens do pedido
        itens = []
        for it in ((raw or {}).get("order_items") or []):
            i = it.get("item") or {}
            itens.append({"sku": i.get("seller_sku") or i.get("seller_custom_field") or i.get("id"),
                          "descricao": i.get("title"), "quantidade": it.get("quantity") or 1,
                          "valor": it.get("unit_price") or 0})
        out["passos"].append(f"2) itens montados: {len(itens)} — {[x['sku'] for x in itens][:3]}")

        # 3) dados fiscais do comprador (billing_info)
        df = ml.dados_fiscais_comprador(order_id, user.id)
        out["billing_info"] = df
        out["passos"].append(f"3) billing_info: ok={df.get('ok')} nome={df.get('nome')} "
                             f"doc={df.get('doc_tipo')} {df.get('doc_numero')} erro={df.get('erro')}")

        # 4) montar o pedido no formato da geração
        pedido = {
            "numero_loja": order_id,
            "cliente": {"nome": df.get("nome"), "cpf_cnpj": df.get("doc_numero"),
                        "endereco": {"completo": df.get("endereco"), "bairro": df.get("bairro"),
                                     "cidade": df.get("cidade"), "uf": df.get("estado"), "cep": df.get("cep")}},
            "itens": itens,
        }
        out["passos"].append("4) pedido montado para a NF-e")

        # 5) molde
        molde = nfe_gerar._molde(user.id)
        out["passos"].append(f"5) molde: {'ENCONTRADO nº ' + str(molde.get('numero')) if molde else 'NENHUM — gere 1 nota no Bling'}")

        # 6) dry-run (NÃO gera nada)
        r = nfe_gerar.gerar(user.id, pedido, molde=molde, dry_run=True)
        out["dry_run"] = r
        out["passos"].append(f"6) dry-run: ok={r.get('ok')} erro={r.get('erro')}")
        out["veredito"] = ("PRONTO PARA GERAR" if r.get("ok")
                           else f"BLOQUEADO: {r.get('erro')}")
    except Exception as e:  # noqa: BLE001
        import traceback
        out["passos"].append(f"ERRO: {type(e).__name__}: {str(e)[:200]}")
        out["trace"] = traceback.format_exc()[-500:]
    return out


# ═══════════════════════════════════════════════════════════════════════════
# PROVA DE EXPEDIÇÃO — Mesa de Separação (mockup v3 aprovado)
# ═══════════════════════════════════════════════════════════════════════════
@app.post("/api/separacao/abrir")
def sep_abrir(payload: dict = Body(default={}), user: User = Depends(auth.get_current_user)):
    """A bipagem da etiqueta chama isto: abre a sessão e a gravação começa."""
    from . import separacao as sp
    db = SessionLocal()
    try:
        s = sp.abrir(db, user.id, payload.get("pedido") or {},
                     bancada=payload.get("bancada") or "Bancada 1",
                     operador=payload.get("operador"),
                     qualidade=payload.get("qualidade") or "padrao")
        return {"ok": True, "sessao": sp.resumo(s)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        db.close()


@app.post("/api/separacao/{sessao_id}/take")
def sep_take(sessao_id: int, payload: dict = Body(default={}),
             user: User = Depends(auth.get_current_user)):
    """Recebe o take já com a marca queimada pelo navegador (dataURL base64)."""
    from . import separacao as sp
    import base64
    db = SessionLocal()
    try:
        b64 = str(payload.get("imagem") or "")
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        dados = base64.b64decode(b64) if b64 else b""
        m = sp.add_take(db, user.id, sessao_id, dados,
                        passo=payload.get("passo") or "avulso",
                        modo=payload.get("modo") or "auto",
                        gatilho=payload.get("gatilho"),
                        largura=payload.get("largura"), altura=payload.get("altura"))
        return {"ok": True, "take": {"id": m.id, "ordem": m.ordem, "passo": m.passo,
                                     "kb": round(m.bytes / 1024, 1), "sha256": m.sha256,
                                     "hash_elo": m.hash_elo, "url": f"/api/separacao/midia/{m.id}"}}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        db.close()


@app.post("/api/separacao/{sessao_id}/evento")
def sep_evento(sessao_id: int, payload: dict = Body(default={}),
               user: User = Depends(auth.get_current_user)):
    """Bipagem de SKU, tique manual, divergência — tudo entra na linha do tempo."""
    from . import separacao as sp
    db = SessionLocal()
    try:
        e = sp.evento(db, user.id, sessao_id, payload.get("tipo") or "evento",
                      descricao=payload.get("descricao"), sku=payload.get("sku"),
                      dados=payload.get("dados"))
        return {"ok": True, "id": e.id}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": str(e)[:200]}
    finally:
        db.close()


@app.post("/api/separacao/{sessao_id}/selar")
def sep_selar(sessao_id: int, payload: dict = Body(default={}),
              user: User = Depends(auth.get_current_user)):
    """Sela o dossiê. Sem os 5 passos do roteiro, NÃO sela."""
    from . import separacao as sp
    db = SessionLocal()
    try:
        return sp.selar(db, user.id, sessao_id, forcar=bool(payload.get("forcar")))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": str(e)[:200]}
    finally:
        db.close()


@app.get("/api/separacao/midia/{midia_id}")
def sep_midia(midia_id: int, user: User = Depends(auth.get_current_user)):
    """Serve a imagem do take (com a marca já queimada)."""
    from .models import SepMidia
    db = SessionLocal()
    try:
        m = (db.query(SepMidia)
             .filter(SepMidia.user_id == user.id, SepMidia.id == midia_id).first())
        if m is None or not m.dados:
            raise HTTPException(status_code=404, detail="take não encontrado")
        return Response(content=m.dados, media_type=m.mime or "image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400",
                                 "X-Take-Sha256": m.sha256})
    finally:
        db.close()


@app.get("/api/separacao/stats")
def sep_stats(user: User = Depends(auth.get_current_user)):
    from . import separacao as sp
    db = SessionLocal()
    try:
        return sp.estatisticas(db, user.id)
    finally:
        db.close()


@app.get("/api/separacao/lista")
def sep_lista(busca: str = "", limite: int = 50, user: User = Depends(auth.get_current_user)):
    from . import separacao as sp
    db = SessionLocal()
    try:
        return {"sessoes": sp.listar(db, user.id, busca=busca, limite=min(limite, 200))}
    finally:
        db.close()


@app.get("/api/separacao/{sessao_id}")
def sep_dossie(sessao_id: int, user: User = Depends(auth.get_current_user)):
    from . import separacao as sp
    db = SessionLocal()
    try:
        d = sp.dossie(db, user.id, sessao_id)
        if not d:
            raise HTTPException(status_code=404, detail="dossiê não encontrado")
        return d
    finally:
        db.close()


@app.get("/api/separacao/{sessao_id}/verificar")
def sep_verificar(sessao_id: int, user: User = Depends(auth.get_current_user)):
    """Recalcula a cadeia inteira — é isto que prova que nada foi trocado."""
    from . import separacao as sp
    db = SessionLocal()
    try:
        return sp.verificar(db, user.id, sessao_id)
    finally:
        db.close()


@app.get("/api/separacao/por-pedido/{pedido_id}")
def sep_por_pedido(pedido_id: str, user: User = Depends(auth.get_current_user)):
    """Existe prova de separação para este pedido? Usado no card lateral do pedido."""
    from . import separacao as sp
    from .models import SepSessao
    db = SessionLocal()
    try:
        s = (db.query(SepSessao)
             .filter(SepSessao.user_id == user.id, SepSessao.pedido_id == str(pedido_id))
             .order_by(SepSessao.aberta_em.desc()).first())
        if s is None:
            return {"tem": False}
        d = sp.dossie(db, user.id, s.id)
        return {"tem": True, "sessao": d.get("sessao"), "takes": d.get("takes"),
                "eventos": d.get("eventos"), "roteiro": d.get("roteiro")}
    finally:
        db.close()


@app.get("/api/separacao/{sessao_id}/pacote")
def sep_pacote(sessao_id: int, user: User = Depends(auth.get_current_user)):
    """Baixa o dossiê inteiro como ZIP: fotos + manifesto com hashes + resumo legível.
    É o arquivo que se envia ao canal numa reclamação."""
    from . import separacao as sp
    from .models import SepMidia, SepEvento as SepEventoRef
    import io as _io, json as _json, zipfile
    db = SessionLocal()
    try:
        d = sp.dossie(db, user.id, sessao_id)
        if not d:
            raise HTTPException(status_code=404, detail="dossiê não encontrado")
        ses = d["sessao"]
        verif = sp.verificar(db, user.id, sessao_id)
        midias = (db.query(SepMidia)
                  .filter(SepMidia.user_id == user.id, SepMidia.sessao_id == sessao_id)
                  .order_by(SepMidia.ordem.asc()).all())
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for m in midias:
                nome = f"{m.ordem:02d}_{m.passo or 'take'}.jpg"
                if m.dados:
                    z.writestr(nome, m.dados)
            # resumo legível para quem abrir o pacote
            linhas = [
                "PROVA DE SEPARAÇÃO E EXPEDIÇÃO",
                "=" * 52, "",
                f"Dossiê:        {ses.get('codigo')}",
                f"Pedido:        {ses.get('pedido_id')} ({'Shopee' if ses.get('canal') == 'shopee' else 'Mercado Livre'})",
                f"Cliente:       {ses.get('cliente') or '—'}",
                f"Documento:     {ses.get('cliente_doc') or '—'}",
                f"Destino:       {ses.get('cidade') or '—'}/{ses.get('uf') or '—'}",
                f"NF-e:          {ses.get('nfe_numero') or '—'}",
                f"Rastreio:      {ses.get('rastreio') or '—'}",
                f"Valor:         R$ {ses.get('valor') or 0:.2f}",
                f"Bancada:       {ses.get('bancada') or '—'}   Operador: {ses.get('operador') or '—'}",
                f"Aberto em:     {ses.get('aberta_em')}",
                f"Selado em:     {ses.get('selada_em')}",
                f"Duração:       {ses.get('duracao_seg') or 0} s",
                "",
                "ITENS DO PEDIDO", "-" * 52,
            ]
            for it in (ses.get("itens") or []):
                linhas.append(f"  {it.get('qtd', 1)}x  {it.get('nome', '')}  [SKU {it.get('sku', '—')}]")
            linhas += ["", "IMAGENS (na ordem do processo)", "-" * 52]
            for m in midias:
                linhas.append(f"  {m.ordem:02d}_{m.passo}.jpg   {m.modo}   {round((m.bytes or 0)/1024,1)} KB")
                linhas.append(f"      impressão digital (SHA-256): {m.sha256}")
                linhas.append(f"      elo da cadeia:               {m.hash_elo}")
            linhas += ["", "LINHA DO TEMPO", "-" * 52]
            for e in (d.get("eventos") or []):
                linhas.append(f"  {str(e.get('quando'))[:19]}  {e.get('tipo'):14s} {e.get('descricao') or ''}")
            linhas += ["", "INTEGRIDADE", "-" * 52,
                       f"  Cadeia íntegra: {'SIM' if verif.get('integra') else 'NÃO'}",
                       f"  Imagens verificadas: {verif.get('takes')}",
                       f"  Hash final: {ses.get('hash_final') or '—'}", "",
                       "Cada imagem tem uma impressão digital que inclui a da imagem anterior.",
                       "Trocar, editar ou remover qualquer arquivo quebra a cadeia e é detectável.",
                       "Os dados do pedido estão gravados na própria imagem.", ""]
            z.writestr("PROVA.txt", "\n".join(linhas))
            z.writestr("manifesto.json", _json.dumps(
                {"sessao": ses, "takes": d.get("takes"), "eventos": d.get("eventos"),
                 "integridade": verif}, ensure_ascii=False, indent=2, default=str))
        # marca como usado em disputa
        try:
            from .models import SepSessao as _SS
            obj = db.query(_SS).filter(_SS.user_id == user.id, _SS.id == sessao_id).first()
            if obj is not None:
                obj.usada_em_disputa = True
                obj.exportada_por = getattr(user, "email", None) or str(user.id)
                obj.exportada_em = datetime.utcnow()
                db.add(SepEventoRef(user_id=user.id, sessao_id=sessao_id, tipo="export",
                                    descricao="dossiê exportado para envio ao canal",
                                    criado_em=datetime.utcnow()))
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
        buf.seek(0)
        nome_zip = f"prova_{ses.get('codigo')}_pedido_{ses.get('pedido_id')}.zip"
        return Response(content=buf.read(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{nome_zip}"'})
    finally:
        db.close()


@app.get("/api/versao")
def versao_backend():
    """Aberto: confirma qual backend está no ar sem depender de logs."""
    return {"backend": "v5.8", "arquitetura": "banco+varredura", "ts": _time.time()}


@app.get("/api/mercadolivre/pedidos-enriquecido")
def ml_pedidos_enriquecido(status: str = "paid", offset: int = 0, limit: int = 30,
                           desde: str = "", ate: str = "",
                           user: User = Depends(auth.get_current_user)):
    def _run(ml):
        try:
            return _pedidos_ml_enriquecidos(ml, user.id, status, offset, limit,
                                            desde or None, ate or None)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            import traceback as _tb
            _t = _tb.format_exc()
            print(f"[pedidos_ml] ERRO FATAL limit={limit} desde={desde}: {type(e).__name__}: {e}\n{_t}", flush=True)
            # devolve o motivo REAL para a tela (sem 500 mudo)
            raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)[:250]}")
    return _ml_run(_run)


# --- Envios / etiqueta real ---
@app.get("/api/mercadolivre/envio/{shipment_id}")
def ml_envio(shipment_id: str, user: User = Depends(auth.get_current_user)):
    def _com_disciplina(ml):
        with _ML_SEM:
            raw = ml.envio_do_pedido(shipment_id, user.id)
        # autocura: persiste no cache de envios — substatus/prazo passam a vir na própria lista
        try:
            from .mercadolivre import _upsert_envio_cache
            _dbu = SessionLocal()
            try:
                _upsert_envio_cache(_dbu, user.id, shipment_id, raw)
                _dbu.commit()
            finally:
                _dbu.close()
        except Exception:  # noqa: BLE001
            pass
        return raw
    return _ml_run(_com_disciplina)


@app.post("/api/mercadolivre/envios/sincronizar")
def ml_envios_sincronizar(payload: dict = Body(default={}), user: User = Depends(auth.get_current_user)):
    """Backfill sob demanda: busca no ML os envios que faltam no cache (até `cap`)
    e grava. O painel chama isso em lotes p/ aquecer os baldes sem travar a lista."""
    ids = payload.get("shipment_ids") or []
    cap = min(int(payload.get("cap") or 60), 120)
    return _ml_run(lambda ml: ml.sincronizar_envios(user.id, ids, cap=cap))


@app.get("/api/mercadolivre/posvenda")
def ml_posvenda(status: str = "opened", user: User = Depends(auth.get_current_user)):
    """Reclamações/devoluções em que o vendedor é parte (balde Devoluções / painel Pós-venda)."""
    return _ml_run(lambda ml: ml.listar_posvenda(user.id, status=status))


@app.get("/api/mercadolivre/posvenda/{claim_id}")
def ml_posvenda_detalhe(claim_id: str, user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.detalhe_posvenda(claim_id, user.id))


@app.get("/api/mercadolivre/tarifa/{order_id}")
def ml_tarifa_detalhe(order_id: str, user: User = Depends(auth.get_current_user)):
    """Composição real da tarifa do pedido (faturamento) — detalhamento sob demanda no drawer."""
    return _ml_run(lambda ml: ml.detalhe_tarifa(order_id, user.id))


@app.get("/api/mercadolivre/mensagens/{pack_id}")
def ml_mensagens(pack_id: str, user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.mensagens_pedido(pack_id, user.id))


@app.post("/api/mercadolivre/mensagens/{pack_id}")
def ml_mensagens_enviar(pack_id: str, payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    buyer_id = payload.get("buyer_id")
    texto = payload.get("texto") or ""
    return _ml_run(lambda ml: ml.enviar_mensagem(pack_id, buyer_id, texto, user.id))


@app.get("/api/mercadolivre/mensagens-nao-lidas")
def ml_mensagens_nao_lidas(user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.mensagens_nao_lidas(user.id))


@app.post("/api/mercadolivre/nfe-status")
def ml_nfe_status(payload: dict = Body(default={}), user: User = Depends(auth.get_current_user)):
    """Casa a NF-e do Bling (módulo fiscal existente) com os pedidos do ML pelo número
    do pedido. Chamado após a lista carregar — não pesa no hot-path (é cacheado)."""
    from . import nfe
    ids = [str(x) for x in (payload.get("order_ids") or []) if x]
    if not ids:
        return {"mapa": {}}
    try:
        mapa = nfe.nfe_por_pedidos(user.id, ids)
    except Exception as e:  # noqa: BLE001
        return {"mapa": {}, "erro": str(e)[:200]}
    out = {}
    for k, v in (mapa or {}).items():
        out[str(k)] = {
            "numero": v.get("nfe_numero"), "serie": v.get("nfe_serie"),
            "emissao": v.get("nfe_emissao"), "valor": v.get("valor_total"),
            "chave": v.get("nfe_chave"), "situacao": v.get("nfe_situacao"),
            "situacao_label": v.get("nfe_situacao_label"),
        }
    return {"mapa": out, "total": len(out)}


@app.get("/api/mercadolivre/dados-fiscais/{order_id}")
def ml_dados_fiscais(order_id: str, user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.dados_fiscais_comprador(order_id, user.id))


@app.get("/api/mercadolivre/envio-extra/{shipment_id}")
def ml_envio_extra(shipment_id: str, order_id: str = None,
                   logistic_type: str = None, ship_status: str = None,
                   user: User = Depends(auth.get_current_user)):
    """Detalhes extra sob demanda (só quando o drawer abre): SLA, transportadora +
    link de rastreio, histórico completo do envio e avaliação do comprador."""
    def _run(ml):
        pular_sla = (ship_status == "cancelled") or (logistic_type == "fulfillment")
        return {
            "sla": ({} if pular_sla else ml.sla_do_shipment(shipment_id, user.id)),
            "carrier": ml.carrier_do_shipment(shipment_id, user.id),
            "historico": ml.historico_do_shipment(shipment_id, user.id),
            "feedback": (ml.feedback_do_pedido(order_id, user.id) if order_id else {}),
        }
    return _ml_run(_run)


@app.get("/api/mercadolivre/coleta")
def ml_coleta(user: User = Depends(auth.get_current_user)):
    """Janela de coleta de hoje (de/até + corte) + transportadora/motorista/veículo, quando o ML expõe.
    Obs.: o Mercado Livre NÃO expõe 'código de autorização de coleta' na API pública (cross_docking usa
    romaneio, e os detalhes ricos ficam num endpoint privado do seller center)."""
    return _ml_run(lambda ml: ml.agenda_coleta(user.id))


@app.get("/api/mercadolivre/etiqueta")
def ml_etiqueta(shipment_ids: str, formato: str = "pdf", user: User = Depends(auth.get_current_user)):
    from . import mercadolivre as ml
    try:
        conteudo, ct = ml.etiqueta(shipment_ids.split(","), formato, user.id)
    except ml.MLNaoConfigurado as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ml.MLErro as e:
        raise HTTPException(status_code=502, detail=f"Mercado Livre: {e}")
    return Response(content=conteudo, media_type=ct)


@app.get("/api/mercadolivre/mensagem-anexo")
def ml_mensagem_anexo(filename: str, user: User = Depends(auth.get_current_user)):
    """Proxy autenticado de anexo de mensagem (imagem/vídeo) para render inline no chat."""
    from . import mercadolivre as ml
    try:
        conteudo, ct = ml.baixar_anexo_mensagem(filename, user.id)
    except ml.MLNaoConfigurado as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Mercado Livre: {e}")
    return Response(content=conteudo, media_type=ct, headers={"Cache-Control": "private, max-age=3600"})


# --- Perguntas ---
@app.get("/api/mercadolivre/perguntas")
def ml_perguntas(status: str = "", item_id: str = "", user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.listar_perguntas(user.id, status or None, item_id or None))


@app.post("/api/mercadolivre/perguntas/responder")
def ml_perguntas_responder(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    qid = payload.get("question_id")
    texto = payload.get("texto")
    if not qid or not texto:
        raise HTTPException(status_code=422, detail="Informe question_id e texto.")
    return _ml_run(lambda ml: ml.responder_pergunta(qid, texto, user.id))


# --- Avaliações ---
@app.get("/api/mercadolivre/avaliacoes/{item_id}")
def ml_avaliacoes(item_id: str, user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.avaliacoes_do_item(item_id, user_id=user.id))


# --- Visitas / funil ---
@app.get("/api/mercadolivre/visitas/{item_id}")
def ml_visitas_item(item_id: str, last: int = 30, unit: str = "day",
                    user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.visitas_do_item(item_id, last, unit, user.id))


@app.get("/api/mercadolivre/visitas")
def ml_visitas_vendedor(desde: str, ate: str, user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.visitas_do_vendedor(desde, ate, user.id))


# --- Promoções (v2) ---
@app.get("/api/mercadolivre/promocoes/painel")
def ml_promocoes_painel(user: User = Depends(auth.get_current_user)):
    """Painel consolidado da Central de Promoções: convites do ML × minhas campanhas,
    com contagens e estado da lista de exclusão (governança)."""
    return _ml_run(lambda ml: ml.painel_promocoes(user.id))


@app.get("/api/mercadolivre/promocoes/promocao/{promotion_id}")
def ml_promocao_detalhe(promotion_id: str, promotion_type: str = Query(...),
                        user: User = Depends(auth.get_current_user)):
    """Detalhe de uma campanha + seus itens (net_proceeds, banda min/max, sugestão do ML)."""
    def _run(ml):
        det = ml.detalhe_promocao(promotion_id, promotion_type, user.id)
        try:
            its = ml.itens_promocao(promotion_id, promotion_type, user_id=user.id)
        except ml.MLErro:
            its = {}
        return {"detalhe": det, "itens": its}
    return _ml_run(_run)


@app.post("/api/mercadolivre/promocoes/simular")
def ml_promocoes_simular(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Simulador de desconto: item + desconto% (ou deal_price) → preço, líquido real,
    custo, lucro real, margem, PISO (Preço Bling) e banda. Não escreve nada no ML."""
    from .models import ProdutoCache, MLItemCache
    item_id = str(payload.get("item_id") or "").strip()
    if not item_id:
        raise HTTPException(status_code=422, detail="Informe item_id.")
    desconto = payload.get("desconto_pct")
    deal_price = payload.get("deal_price")
    db = SessionLocal()
    try:
        mlc = db.query(MLItemCache).filter(MLItemCache.user_id == user.id,
                                           MLItemCache.item_id == item_id).first()
        if not mlc:
            raise HTTPException(status_code=404, detail="Item não está no cache do Mercado Livre.")
        preco_atual = float(mlc.preco or 0)
        titulo, sku = mlc.titulo, mlc.sku
        prod = None
        if sku:
            prod = db.query(ProdutoCache).filter(ProdutoCache.user_id == user.id,
                                                 ProdutoCache.sku == sku).first()
        preco_bling = float(prod.preco) if (prod and prod.preco) else None
        custo = float(prod.custo) if (prod and prod.custo) else None
    finally:
        db.close()
    if deal_price is None:
        d = float(desconto or 0)
        deal_price = round(preco_atual * (1 - d / 100.0), 2)
    else:
        deal_price = round(float(deal_price), 2)
        desconto = round((1 - deal_price / preco_atual) * 100, 1) if preco_atual else None
    cfg = precificacao.obter_config(user.id)
    mr = precificacao.margem_real_canal(cfg, "mercadolivre", deal_price, custo) or {}
    liquido = mr.get("liquido")
    piso_preco = (precificacao.preco_minimo_para_liquido(cfg, "mercadolivre", preco_bling)
                  if preco_bling is not None else None)
    acima_do_piso = (liquido >= preco_bling) if (liquido is not None and preco_bling is not None) else None
    sug = None
    try:
        from . import mercadolivre as _ml
        sug = _ml.sugestao_desconto_item(item_id, preco_atual, user.id)
    except Exception:  # noqa: BLE001
        sug = None
    return {
        "item_id": item_id, "titulo": titulo, "sku": sku,
        "preco_atual": round(preco_atual, 2), "desconto_pct": desconto, "deal_price": deal_price,
        "liquido": liquido, "taxas": mr.get("taxas"), "custo": custo,
        "lucro": mr.get("lucro"), "margem_pct": mr.get("margem_pct"),
        "preco_bling": preco_bling, "piso_preco": piso_preco, "acima_do_piso": acima_do_piso,
        "sugestao_preco": (sug or {}).get("preco"), "sugestao_pct": (sug or {}).get("pct"),
    }


# --- Escrita de promoções (com TRAVA DE MARGEM no Preço Bling) ---
def _piso_item(user_id, item_id):
    """(preco_bling, piso_preco) do item — piso = menor preço cujo líquido cobre o Preço Bling."""
    from .models import ProdutoCache, MLItemCache
    db = SessionLocal()
    try:
        mlc = db.query(MLItemCache).filter(MLItemCache.user_id == user_id, MLItemCache.item_id == item_id).first()
        sku = mlc.sku if mlc else None
        prod = (db.query(ProdutoCache).filter(ProdutoCache.user_id == user_id, ProdutoCache.sku == sku).first()
                if sku else None)
        preco_bling = float(prod.preco) if (prod and prod.preco) else None
    finally:
        db.close()
    cfg = precificacao.obter_config(user_id)
    piso = (precificacao.preco_minimo_para_liquido(cfg, "mercadolivre", preco_bling)
            if preco_bling is not None else None)
    return preco_bling, piso


def _checa_piso(user_id, item_id, deal_price):
    """Bloqueia (HTTP 400) se deal_price fura o piso (Preço Bling). A regra de ouro do módulo."""
    _pb, piso = _piso_item(user_id, item_id)
    if piso is not None and deal_price is not None and float(deal_price) < piso:
        raise HTTPException(status_code=400, detail=(
            f"Bloqueado pela trava de margem: R$ {float(deal_price):.2f} fica abaixo do piso "
            f"R$ {piso:.2f} (Preço Bling). Reduza o desconto."))


@app.post("/api/mercadolivre/promocoes/campanha")
def ml_promo_criar_campanha(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    nome, ini, fim = payload.get("nome"), payload.get("inicio"), payload.get("fim")
    if not (nome and ini and fim):
        raise HTTPException(status_code=422, detail="Informe nome, inicio e fim.")
    return _ml_run(lambda ml: ml.criar_campanha_percentual(nome, ini, fim, user.id))


@app.post("/api/mercadolivre/promocoes/cupom")
def ml_promo_criar_cupom(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    obr = ("nome", "inicio", "fim", "subtipo", "valor", "min_compra")
    if any(payload.get(k) in (None, "") for k in obr):
        raise HTTPException(status_code=422, detail="Informe nome, inicio, fim, subtipo, valor e min_compra.")
    return _ml_run(lambda ml: ml.criar_cupom(
        payload["nome"], payload["inicio"], payload["fim"], payload["subtipo"], payload["valor"],
        payload["min_compra"], payload.get("codigo"), payload.get("orcamento"), payload.get("max_desconto"), user.id))


@app.post("/api/mercadolivre/promocoes/volume")
def ml_promo_criar_volume(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    obr = ("nome", "inicio", "fim", "subtipo", "buy_quantity")
    if any(payload.get(k) in (None, "") for k in obr):
        raise HTTPException(status_code=422, detail="Informe nome, inicio, fim, subtipo e buy_quantity.")
    return _ml_run(lambda ml: ml.criar_volume(
        payload["nome"], payload["inicio"], payload["fim"], payload["subtipo"], payload["buy_quantity"],
        payload.get("pay_quantity"), payload.get("discount_percentage"),
        payload.get("allow_combination", True), user.id))


@app.put("/api/mercadolivre/promocoes/campanha/{promotion_id}")
def ml_promo_editar_campanha(promotion_id: str, payload: dict = Body(...),
                             user: User = Depends(auth.get_current_user)):
    ptype = payload.get("promotion_type")
    if not ptype:
        raise HTTPException(status_code=422, detail="Informe promotion_type.")
    campos = payload.get("campos") or {k: v for k, v in payload.items() if k not in ("promotion_type", "campos")}
    return _ml_run(lambda ml: ml.editar_campanha(promotion_id, ptype, campos, user.id))


@app.delete("/api/mercadolivre/promocoes/campanha/{promotion_id}")
def ml_promo_excluir_campanha(promotion_id: str, promotion_type: str = Query(...),
                              user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.excluir_campanha(promotion_id, promotion_type, user.id))


@app.post("/api/mercadolivre/promocoes/item/{item_id}/desconto")
def ml_promo_desconto(item_id: str, payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    deal = payload.get("deal_price")
    if deal is None:
        raise HTTPException(status_code=422, detail="Informe deal_price.")
    _checa_piso(user.id, item_id, deal)
    return _ml_run(lambda ml: ml.criar_desconto_item(
        item_id, deal, payload.get("inicio"), payload.get("fim"), payload.get("top_deal_price"), user.id))


@app.delete("/api/mercadolivre/promocoes/item/{item_id}/desconto")
def ml_promo_desconto_remover(item_id: str, user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.remover_item_promocao(item_id, "PRICE_DISCOUNT", user_id=user.id))


@app.post("/api/mercadolivre/promocoes/item/{item_id}/aderir")
def ml_promo_aderir(item_id: str, payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    pid, ptype = payload.get("promotion_id"), payload.get("promotion_type")
    if not (pid and ptype):
        raise HTTPException(status_code=422, detail="Informe promotion_id e promotion_type.")
    deal = payload.get("deal_price")
    # Trava de margem ativa por padrão. Só é ignorada com override manual explícito
    # (decisão consciente do operador — ex.: loss-leader de Relâmpago para giro/visibilidade).
    if deal is not None and not payload.get("permitir_abaixo_piso"):
        _checa_piso(user.id, item_id, deal)
    return _ml_run(lambda ml: ml.add_item_promocao(
        item_id, pid, ptype, deal, payload.get("top_deal_price"), payload.get("stock"),
        payload.get("offer_id"), user.id))


@app.delete("/api/mercadolivre/promocoes/item/{item_id}")
def ml_promo_remover_item(item_id: str, promotion_type: str = Query(...),
                          promotion_id: str = Query(None), offer_id: str = Query(None),
                          user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.remover_item_promocao(item_id, promotion_type, promotion_id, offer_id, user.id))


@app.post("/api/mercadolivre/promocoes/promocao/{promotion_id}/sair")
def ml_promo_sair(promotion_id: str, promotion_type: str = Query(...),
                  user: User = Depends(auth.get_current_user)):
    """Deixa de aderir: remove TODOS os itens participando (status ativo) de um convite/campanha."""
    base, _tr, _sb = _itens_convite_enriquecidos(user.id, promotion_id, promotion_type)
    participando = [it for it in base if (it.get("status") or "").lower() in ("active", "started", "enabled")]
    resumo = {"removidos": 0, "falhas": [], "participando": len(participando)}

    def _run(ml):
        for it in participando:
            try:
                ml.remover_item_promocao(it["item_id"], promotion_type, promotion_id, it.get("offer_id"), user.id)
                resumo["removidos"] += 1
            except Exception as e:  # noqa: BLE001
                resumo["falhas"].append({"item_id": it["item_id"], "erro": str(e)[:140]})
        return None

    _ml_run(lambda ml: _run(ml))
    return resumo


@app.post("/api/mercadolivre/promocoes/exclusao/seller")
def ml_promo_exclusao_seller(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.exclusao_seller_set(bool(payload.get("ativo")), user.id))


@app.post("/api/mercadolivre/promocoes/exclusao/item")
def ml_promo_exclusao_item(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    iid = payload.get("item_id")
    if not iid:
        raise HTTPException(status_code=422, detail="Informe item_id.")
    return _ml_run(lambda ml: ml.exclusao_item_set(iid, bool(payload.get("ativo")), user.id))


def _npx(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


@app.get("/api/mercadolivre/promocoes/itens")
def ml_promo_itens(q: str = Query(None), limit: int = Query(30, le=60), offset: int = Query(0),
                   apenas_ativos: bool = Query(True), user: User = Depends(auth.get_current_user)):
    """Anúncios do vendedor (cache) com Preço Bling + piso — alimenta o seletor de itens (modo campanha)."""
    from sqlalchemy import or_ as _or
    from .models import MLItemCache, ProdutoCache
    db = SessionLocal()
    try:
        query = db.query(MLItemCache).filter(MLItemCache.user_id == user.id)
        if apenas_ativos:
            query = query.filter(MLItemCache.status == "active")
        if q and q.strip():
            like = f"%{q.strip()}%"
            query = query.filter(_or(MLItemCache.titulo.ilike(like), MLItemCache.sku.ilike(like),
                                     MLItemCache.item_id.ilike(like)))
        total = query.count()
        rows = query.order_by(MLItemCache.titulo.asc()).offset(offset).limit(limit).all()
        skus = [r.sku for r in rows if r.sku]
        prods = ({p.sku: p for p in db.query(ProdutoCache).filter(
                  ProdutoCache.user_id == user.id, ProdutoCache.sku.in_(skus)).all()} if skus else {})
    finally:
        db.close()
    cfg = precificacao.obter_config(user.id)
    itens = []
    for r in rows:
        prod = prods.get(r.sku) if r.sku else None
        preco_bling = float(prod.preco) if (prod and prod.preco and prod.preco > 0) else None
        custo = float(prod.custo) if (prod and prod.custo and prod.custo > 0) else None
        piso = (precificacao.preco_minimo_para_liquido(cfg, "mercadolivre", preco_bling)
                if preco_bling is not None else None)
        itens.append({
            "item_id": r.item_id, "sku": r.sku, "titulo": r.titulo, "preco": float(r.preco or 0),
            "imagem": r.imagem, "status": r.status, "estoque": r.estoque,
            "logistic_type": r.logistic_type, "em_promocao": bool(r.em_promocao),
            "preco_bling": preco_bling, "custo": custo, "piso_preco": piso,
        })
    return {"total": total, "offset": offset, "limit": limit, "itens": itens}


_TIPOS_OFFER_ID = {"LIGHTNING", "SMART", "PRE_NEGOTIATED", "UNHEALTHY_STOCK", "BANK", "PRICE_MATCHING", "PRICE_MATCHING_MELI_ALL"}


def _deal_convite(it, pct):
    """Preço do item no convite: original - pct%, grampeado na banda do ML
    (max_discounted_price = piso da banda; min_discounted_price = teto)."""
    dp = (it.get("original_price") or 0) * (1 - float(pct) / 100)
    maxd, mind = it.get("max_discounted_price"), it.get("min_discounted_price")
    if maxd is not None:
        dp = max(dp, maxd)
    if mind is not None:
        dp = min(dp, mind)
    return round(dp, 2)


def _itens_convite_enriquecidos(user_id, promotion_id, promotion_type):
    """Pagina + normaliza + enriquece (título/imagem/estoque/sku) + piso do Bling para os
    itens/candidatos de um convite. Retorna (base, truncado, sem_bling)."""
    from . import mercadolivre as _ml
    todos, sa, truncado = [], None, False
    for i in range(12):
        raw = _ml_run(lambda ml, _sa=sa: ml.itens_promocao(
            promotion_id, promotion_type, user_id=user_id, limit=50, search_after=_sa)) or {}
        lst = (raw.get("results") if isinstance(raw, dict) else raw) or []
        todos.extend(lst)
        paging = (raw.get("paging") if isinstance(raw, dict) else {}) or {}
        sa = paging.get("searchAfter") or paging.get("search_after")
        if not sa or len(lst) < 50:
            break
        if i == 11:
            truncado = True
    base = []
    for it in todos:
        iid = it.get("id") or it.get("item_id")
        if not iid:
            continue
        stock = it.get("stock") if isinstance(it.get("stock"), dict) else {}
        np_ = it.get("net_proceeds") if isinstance(it.get("net_proceeds"), dict) else {}
        base.append({
            "item_id": iid,
            "original_price": _npx(it.get("original_price")) or _npx(it.get("price")),
            "price": _npx(it.get("price")),
            "meli_percentage": _npx(it.get("meli_percentage")),
            "seller_percentage": _npx(it.get("seller_percentage")),
            "min_discounted_price": _npx(it.get("min_discounted_price")),
            "max_discounted_price": _npx(it.get("max_discounted_price")),
            "suggested_discounted_price": _npx(it.get("suggested_discounted_price")),
            "top_deal_price": _npx(it.get("top_deal_price")),
            "net_proceeds": _npx(np_.get("amount")),
            "stock_min": stock.get("min"), "stock_max": stock.get("max"),
            "offer_id": it.get("offer_id") or (((it.get("offers") or [{}])[0] or {}).get("id")),
            "status": it.get("status"),
        })
    ids = [b["item_id"] for b in base]
    from .models import MLItemCache, ProdutoCache
    db = SessionLocal()
    try:
        mlc = ({m.item_id: m for m in db.query(MLItemCache).filter(
                MLItemCache.user_id == user_id, MLItemCache.item_id.in_(ids)).all()} if ids else {})
        skus = [m.sku for m in mlc.values() if m.sku]
        prods = ({p.sku: p for p in db.query(ProdutoCache).filter(
                  ProdutoCache.user_id == user_id, ProdutoCache.sku.in_(skus)).all()} if skus else {})
    finally:
        db.close()
    faltantes = [b["item_id"] for b in base if not (mlc.get(b["item_id"]) and mlc[b["item_id"]].titulo)]
    extra = {}
    if faltantes:
        try:
            for x in _ml.obter_itens(faltantes, user_id,
                                     attributes="id,title,thumbnail,pictures,available_quantity,price,seller_custom_field,attributes,status"):
                if x.get("item_id"):
                    extra[x["item_id"]] = x
        except Exception:  # noqa: BLE001
            extra = {}
    skus_extra = [x.get("sku") for x in extra.values() if x.get("sku") and x.get("sku") not in prods]
    if skus_extra:
        db2 = SessionLocal()
        try:
            for p in db2.query(ProdutoCache).filter(
                    ProdutoCache.user_id == user_id, ProdutoCache.sku.in_(skus_extra)).all():
                if p.sku:
                    prods[p.sku] = p
        finally:
            db2.close()
    cfg = precificacao.obter_config(user_id)
    for b in base:
        m = mlc.get(b["item_id"]); x = extra.get(b["item_id"])
        b["titulo"] = (m.titulo if (m and m.titulo) else None) or (x.get("titulo") if x else None)
        b["imagem"] = (m.imagem if (m and m.imagem) else None) or (x.get("imagem") if x else None)
        sku = (m.sku if (m and m.sku) else None) or (x.get("sku") if x else None)
        b["sku"] = sku
        est = (m.estoque if (m and m.estoque is not None) else None)
        if est is None and x is not None:
            est = x.get("estoque")
        b["estoque"] = est
        prod = prods.get(sku) if sku else None
        preco_bling = float(prod.preco) if (prod and prod.preco and prod.preco > 0) else None
        b["preco"] = float(m.preco) if (m and m.preco and m.preco > 0) else (x.get("preco") if x else None)
        b["preco_bling"] = preco_bling
        b["piso_preco"] = (precificacao.preco_minimo_para_liquido(cfg, "mercadolivre", preco_bling)
                           if preco_bling is not None else None)
    sem_bling = sum(1 for b in base if b["preco_bling"] is None)
    return base, truncado, sem_bling


@app.get("/api/mercadolivre/promocoes/promocao/{promotion_id}/itens")
def ml_promo_promocao_itens(promotion_id: str, promotion_type: str = Query(...),
                            user: User = Depends(auth.get_current_user)):
    """Itens candidatos/participantes de um convite (paginado — TODOS), com banda de preço, piso e enriquecimento."""
    base, truncado, sem_bling = _itens_convite_enriquecidos(user.id, promotion_id, promotion_type)
    return {"promotion_id": promotion_id, "promotion_type": promotion_type, "itens": base,
            "total": len(base), "truncado": truncado, "sem_preco_bling": sem_bling}


@app.post("/api/mercadolivre/promocoes/promocao/{promotion_id}/aderir-auto")
def ml_promo_aderir_auto(promotion_id: str, promotion_type: str = Query(...),
                         desconto_pct: float = Query(15, ge=5, le=80),
                         user: User = Depends(auth.get_current_user)):
    """Auto-adesão de um convite: adere todos os itens elegíveis cujo preço fica ACIMA do piso
    (a automação NUNCA fura a margem). Pula os que furam o piso e os sem Preço Bling."""
    base, _tr, _sb = _itens_convite_enriquecidos(user.id, promotion_id, promotion_type)
    resumo = {"aderidos": 0, "ignorados_piso": 0, "sem_preco_bling": 0, "ja_participando": 0,
              "falhas": [], "total_candidatos": len(base)}

    def _run(ml):
        for it in base:
            if (it.get("status") or "").lower() in ("active", "started", "enabled"):
                resumo["ja_participando"] += 1
                continue
            piso = it.get("piso_preco")
            if piso is None:
                resumo["sem_preco_bling"] += 1
                continue
            dp = _deal_convite(it, desconto_pct)
            if dp < piso:
                resumo["ignorados_piso"] += 1
                continue
            offer_id = it.get("offer_id") if promotion_type in _TIPOS_OFFER_ID else None
            stock = None
            if promotion_type == "LIGHTNING":
                lo = (it.get("stock_min") + 1) if it.get("stock_min") is not None else 1
                hi = (it.get("stock_max") - 1) if it.get("stock_max") is not None else None
                sv = it.get("estoque") if it.get("estoque") is not None else lo
                sv = max(sv, lo)
                if hi is not None:
                    sv = min(sv, max(hi, lo))
                stock = max(1, int(round(sv)))
            try:
                ml.add_item_promocao(it["item_id"], promotion_id, promotion_type, dp, None, stock, offer_id, user.id)
                resumo["aderidos"] += 1
            except Exception as e:  # noqa: BLE001
                resumo["falhas"].append({"item_id": it["item_id"], "erro": str(e)[:140]})
        return None

    _ml_run(lambda ml: _run(ml))
    return resumo


@app.get("/api/mercadolivre/promocoes/promocao/{promotion_id}/contagem")
def ml_promo_contagem(promotion_id: str, promotion_type: str = Query(...),
                      user: User = Depends(auth.get_current_user)):
    """Contagem leve: total de itens/candidatos (elegíveis) + quantos participando (status ativo).
    Sem enriquecimento — só para os cards mostrarem elegíveis × participando."""
    def _run(ml):
        total, participando, sa = 0, 0, None
        for _ in range(20):
            raw = ml.itens_promocao(promotion_id, promotion_type, user_id=user.id, limit=50, search_after=sa) or {}
            lst = (raw.get("results") if isinstance(raw, dict) else raw) or []
            total += len(lst)
            for it in lst:
                if (it.get("status") or "").lower() in ("active", "started", "enabled"):
                    participando += 1
            paging = (raw.get("paging") if isinstance(raw, dict) else {}) or {}
            sa = paging.get("searchAfter") or paging.get("search_after")
            if not sa or len(lst) < 50:
                break
        return {"total": total, "participando": participando}
    return _ml_run(lambda ml: _run(ml))


@app.get("/api/mercadolivre/promocoes/item/{item_id}/promocoes")
def ml_item_promocoes(item_id: str, user: User = Depends(auth.get_current_user)):
    """Todas as promoções de UM anúncio (como o painel por-produto do ML): desconto, preço final,
    você recebe (líquido estimado), estado e vigência — para ver em quais campanhas o produto está."""
    from .models import MLItemCache, ProdutoCache
    db = SessionLocal()
    try:
        mi = db.query(MLItemCache).filter(MLItemCache.user_id == user.id, MLItemCache.item_id == item_id).first()
        titulo = mi.titulo if mi else None
        sku = mi.sku if mi else None
        preco_cat = float(mi.preco) if (mi and mi.preco) else None
        imagem = mi.imagem if mi else None
    finally:
        db.close()
    if not titulo or not imagem or not sku:  # não está no cache → busca no ML
        fetched = _ml_run(lambda ml: ml.obter_itens(
            [item_id], user.id, attributes="id,title,price,thumbnail,pictures,seller_custom_field,attributes")) or []
        if fetched:
            f = fetched[0]
            titulo = titulo or f.get("titulo")
            sku = sku or f.get("sku")
            preco_cat = preco_cat if preco_cat is not None else f.get("preco")
            imagem = imagem or f.get("imagem")
    titulo = titulo or item_id
    preco_bling = custo = None
    if sku:
        db = SessionLocal()
        try:
            prod = db.query(ProdutoCache).filter(ProdutoCache.user_id == user.id, ProdutoCache.sku == sku).first()
            preco_bling = float(prod.preco) if (prod and prod.preco and prod.preco > 0) else None
            custo = float(prod.custo) if (prod and prod.custo and prod.custo > 0) else None
        finally:
            db.close()
    cfg = precificacao.obter_config(user.id)
    piso = precificacao.preco_minimo_para_liquido(cfg, "mercadolivre", preco_bling) if preco_bling is not None else None

    def _run(ml):
        raw = ml.promocoes_do_item(item_id, user.id) or {}
        lst = raw.get("results") if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
        painel = ml.painel_promocoes(user.id) or {}
        nomes = {}
        for p in (painel.get("convites") or []) + (painel.get("minhas") or []):
            if p.get("id"):
                nomes[p["id"]] = {"nome": p.get("name"), "deadline": p.get("deadline_date")}
        out = []
        for pr in (lst or []):
            pid = pr.get("id") or pr.get("promotion_id")
            price = _npx(pr.get("price"))
            original = _npx(pr.get("original_price")) or preco_cat
            coop = (_npx(pr.get("meli_percentage")) or 0) + (_npx(pr.get("seller_percentage")) or 0)
            desc = round(coop, 1) if coop > 0 else (round((1 - price / original) * 100, 1)
                                                    if (original and price and price < original) else 0)
            liq = None
            if price:
                liq = (precificacao.margem_real_canal(cfg, "mercadolivre", price, custo) or {}).get("liquido")
            info = nomes.get(pid, {})
            # offer_id: LIGHTNING/SMART/etc. exigem o candidato. O ML o entrega em offers[]/candidate/ref_id.
            offer_id = pr.get("offer_id") or pr.get("candidate_id") or pr.get("ref_id")
            offers = pr.get("offers") or []
            if not offer_id and offers:
                cand = next((o for o in offers
                             if (o.get("status") or "").lower() in ("candidate", "available", "started", "pending")),
                            offers[0])
                offer_id = cand.get("id") or cand.get("offer_id")
            out.append({
                "id": pid, "nome": info.get("nome") or pr.get("name") or pr.get("type"), "type": pr.get("type"),
                "sub_type": pr.get("sub_type"), "status": pr.get("status"),
                "start_date": pr.get("start_date"), "finish_date": pr.get("finish_date"),
                "deadline_date": info.get("deadline"), "preco_final": price, "original": original,
                "desconto_pct": desc, "meli_percentage": _npx(pr.get("meli_percentage")),
                "seller_percentage": _npx(pr.get("seller_percentage")), "voce_recebe": liq, "piso": piso,
                "offer_id": offer_id,
                "acima_piso": (piso is None or (price is not None and price >= piso)),
            })
        out.sort(key=lambda x: (x["status"] or "").lower() not in ("active", "started", "enabled"))
        return out

    proms = _ml_run(lambda ml: _run(ml))
    from .models import MLPedidoItemCache
    corte = datetime.utcnow() - timedelta(days=30)
    db = SessionLocal()
    try:
        filtro = [MLPedidoItemCache.user_id == user.id,
                  MLPedidoItemCache.date_created >= corte,
                  MLPedidoItemCache.status.notin_(["cancelled", "invalid"])]
        if sku:
            from sqlalchemy import or_
            filtro.append(or_(MLPedidoItemCache.item_id == item_id, MLPedidoItemCache.sku == sku))
        else:
            filtro.append(MLPedidoItemCache.item_id == item_id)
        rows = db.query(MLPedidoItemCache).filter(*filtro).all()
    finally:
        db.close()
    vistos = set()
    vendas_30d, receita_30d = 0, 0.0
    for r in rows:
        chave = (r.order_id, r.item_id or r.sku)
        if chave in vistos:
            continue
        vistos.add(chave)
        vendas_30d += r.quantidade or 0
        receita_30d += r.receita or 0.0
    return {"item_id": item_id, "titulo": titulo, "sku": sku, "imagem": imagem,
            "vendas_30d": vendas_30d, "receita_30d": round(receita_30d, 2),
            "preco": preco_cat, "preco_bling": preco_bling, "piso": piso,
            "participando": sum(1 for p in proms if (p.get("status") or "").lower() in ("active", "started", "enabled")),
            "promocoes": proms}


@app.get("/api/mercadolivre/promocoes/item/{item_id}/offer")
def ml_item_offer(item_id: str, promotion_id: str = Query(...), promotion_type: str = Query(...),
                  user: User = Depends(auth.get_current_user)):
    """Resolve o offer_id (candidato) de UM item numa promoção — para tipos que exigem offer_id na adesão."""
    def _run(ml):
        sa = None
        for _ in range(8):
            raw = ml.itens_promocao(promotion_id, promotion_type, user_id=user.id, limit=50, search_after=sa) or {}
            lst = (raw.get("results") if isinstance(raw, dict) else raw) or []
            for it in lst:
                iid = it.get("id") or it.get("item_id")
                if iid == item_id:
                    off = it.get("offer_id") or it.get("candidate_id") or it.get("ref_id")
                    offers = it.get("offers") or []
                    if not off and offers:
                        cand = next((o for o in offers
                                     if (o.get("status") or "").lower()
                                     in ("candidate", "available", "started", "pending")), offers[0])
                        off = cand.get("id") or cand.get("offer_id")
                    return {"offer_id": off, "found": True}
            paging = (raw.get("paging") if isinstance(raw, dict) else {}) or {}
            sa = paging.get("searchAfter") or paging.get("search_after")
            if not sa or len(lst) < 50:
                break
        return {"offer_id": None, "found": False}
    return _ml_run(lambda ml: _run(ml))


_PART_CACHE = {}
_PART_TTL = 300  # 5 min


@app.get("/api/mercadolivre/promocoes/participantes")
def ml_promo_participantes(forcar: bool = Query(False), dias: int = Query(30, ge=1, le=365),
                           mes: str = Query(None), user: User = Depends(auth.get_current_user)):
    """Produtos que estão participando de campanhas, agrupados — com os títulos das campanhas.
    Vendas na janela escolhida: `dias` (7/15/30/60/90...) OU `mes` (YYYY-MM). Cache 5 min por período."""
    chave = (user.id, mes or f"d{dias}")
    ent = _PART_CACHE.get(chave)
    if ent and not forcar and (datetime.utcnow() - ent["ts"]).total_seconds() < _PART_TTL:
        d = dict(ent["data"]); d["cache"] = True; d["atualizado_em"] = ent["ts"].isoformat()
        return d
    painel = _ml_run(lambda ml: ml.painel_promocoes(user.id)) or {}
    promos = (painel.get("convites") or []) + (painel.get("minhas") or [])
    prod_map = {}
    cfg = precificacao.obter_config(user.id)
    camp_fin = {}

    def _run(ml):
        for p in promos[:30]:
            pid, ptype, pname = p.get("id"), p.get("type"), p.get("name")
            if not pid:
                continue
            sa = None
            for _ in range(4):
                raw = ml.itens_promocao(pid, ptype, user_id=user.id, limit=50, search_after=sa) or {}
                lst = (raw.get("results") if isinstance(raw, dict) else raw) or []
                for it in lst:
                    if (it.get("status") or "").lower() in ("active", "started", "enabled"):
                        iid = it.get("id") or it.get("item_id")
                        if iid:
                            e = prod_map.setdefault(iid, {"campanhas": [], "ids": set(),
                                                          "desconto_max": 0.0, "preco_promo": None})
                            if pid not in e["ids"]:
                                e["ids"].add(pid)
                                e["campanhas"].append({"id": pid, "nome": pname, "type": ptype})
                            _pr = _npx(it.get("price"))
                            _og = _npx(it.get("original_price"))
                            if _pr and _og and _og > _pr:
                                _d = round((1 - _pr / _og) * 100, 1)
                                if _d > e["desconto_max"]:
                                    e["desconto_max"] = _d
                                    e["preco_promo"] = _pr
                            cf = camp_fin.setdefault(pid, {"id": pid, "nome": pname, "type": ptype,
                                                           "start_date": p.get("start_date"),
                                                           "finish_date": p.get("finish_date"),
                                                           "n": 0, "voce_recebe": 0.0, "desconto": 0.0,
                                                           "itens": set()})
                            cf["n"] += 1
                            cf["itens"].add(iid)
                            price = _npx(it.get("price"))
                            original = _npx(it.get("original_price"))
                            if price:
                                liq = (precificacao.margem_real_canal(cfg, "mercadolivre", price, None) or {}).get("liquido")
                                if liq:
                                    cf["voce_recebe"] += liq
                            if price and original and original > price:
                                cf["desconto"] += (original - price)
                paging = (raw.get("paging") if isinstance(raw, dict) else {}) or {}
                sa = paging.get("searchAfter") or paging.get("search_after")
                if not sa or len(lst) < 50:
                    break
        return None

    _ml_run(lambda ml: _run(ml))
    ids = list(prod_map.keys())
    from .models import MLItemCache
    db = SessionLocal()
    try:
        mlc = ({m.item_id: m for m in db.query(MLItemCache).filter(
                MLItemCache.user_id == user.id, MLItemCache.item_id.in_(ids)).all()} if ids else {})
    finally:
        db.close()
    faltando = [iid for iid in ids if iid not in mlc or not mlc[iid].titulo]
    extra = (_ml_run(lambda ml: ml.obter_itens(
        faltando, user.id,
        attributes="id,title,price,thumbnail,pictures,seller_custom_field,attributes,"
                   "available_quantity,shipping,health,catalog_listing,catalog_product_id"))
        if faltando else [])
    extra_map = {e["item_id"]: e for e in (extra or [])}
    # vendas reais (cache de pedidos, 30d) por item_id e por SKU
    from .models import MLPedidoItemCache
    if mes:
        try:
            ano_m, mes_m = int(mes[:4]), int(mes[5:7])
        except (ValueError, IndexError):
            raise HTTPException(status_code=400, detail="Parâmetro mes deve ser YYYY-MM.")
        corte = datetime(ano_m, mes_m, 1)
        fim_janela = datetime(ano_m + 1, 1, 1) if mes_m == 12 else datetime(ano_m, mes_m + 1, 1)
        rotulo_janela = f"{mes_m:02d}/{ano_m}"
    else:
        corte = datetime.utcnow() - timedelta(days=dias)
        fim_janela = None
        rotulo_janela = f"{dias}d"
    vendas_por_item, vendas_por_sku = {}, {}
    if ids:
        db = SessionLocal()
        try:
            qq = (db.query(MLPedidoItemCache)
                  .filter(MLPedidoItemCache.user_id == user.id,
                          MLPedidoItemCache.date_created >= corte,
                          MLPedidoItemCache.status.notin_(["cancelled", "invalid"])))
            if fim_janela is not None:
                qq = qq.filter(MLPedidoItemCache.date_created < fim_janela)
            rows = qq.all()
        finally:
            db.close()
        for r in rows:
            if r.item_id:
                v = vendas_por_item.setdefault(r.item_id, {"un": 0, "receita": 0.0})
                v["un"] += r.quantidade or 0
                v["receita"] += r.receita or 0.0
            if r.sku:
                v2 = vendas_por_sku.setdefault(r.sku, {"un": 0, "receita": 0.0})
                v2["un"] += r.quantidade or 0
                v2["receita"] += r.receita or 0.0
    tem_pedidos = bool(vendas_por_item or vendas_por_sku)
    produtos = []
    for iid, v in prod_map.items():
        m = mlc.get(iid)
        ex = extra_map.get(iid, {})
        _sku = (m.sku if m else None) or ex.get("sku")
        _v30 = vendas_por_item.get(iid) or (vendas_por_sku.get(_sku) if _sku else None) or {"un": 0, "receita": 0.0}
        produtos.append({"item_id": iid,
                         "titulo": (m.titulo if m else None) or ex.get("titulo") or iid,
                         "sku": (m.sku if m else None) or ex.get("sku"),
                         "imagem": (m.imagem if m else None) or ex.get("imagem"),
                         "preco": (float(m.preco) if (m and m.preco) else ex.get("preco")),
                         "estoque": (m.estoque if (m and m.estoque is not None) else ex.get("estoque")),
                         "logistic_type": (m.logistic_type if m else None) or ex.get("logistic_type"),
                         "frete_gratis": ex.get("frete_gratis"),
                         "catalogo": ex.get("catalogo"),
                         "saude": (float(m.saude) if (m and m.saude is not None) else ex.get("health")),
                         "listing_type": (m.listing_type_id if m else None),
                         "permalink": (m.permalink if m else None),
                         "desconto_max_pct": v.get("desconto_max") or 0,
                         "preco_promo": v.get("preco_promo"),
                         "vendas_30d": _v30["un"], "receita_30d": round(_v30["receita"], 2),
                         "campanhas": v["campanhas"], "n": len(v["campanhas"])})
    produtos.sort(key=lambda x: x["n"], reverse=True)

    # ---- Radar de eficiência: giro DURANTE a campanha × período ANTERIOR igual ----
    if tem_pedidos and camp_fin:
        agora = datetime.utcnow()
        corte_lift = agora - timedelta(days=180)
        db = SessionLocal()
        try:
            rows_all = (db.query(MLPedidoItemCache)
                        .filter(MLPedidoItemCache.user_id == user.id,
                                MLPedidoItemCache.date_created >= corte_lift,
                                MLPedidoItemCache.status.notin_(["cancelled", "invalid"]))
                        .all())
        finally:
            db.close()
        por_item_datas = {}
        for r in rows_all:
            if r.item_id and r.date_created:
                por_item_datas.setdefault(r.item_id, []).append((r.date_created, r.quantidade or 0))

        def _pdt(x):
            try:
                return datetime.fromisoformat(str(x)[:19])
            except (ValueError, TypeError):
                return None

        for cf in camp_fin.values():
            ini = _pdt(cf.get("start_date"))
            fim_c = _pdt(cf.get("finish_date"))
            itens_c = cf.get("itens") or set()
            if not ini or not itens_c or ini > agora:
                cf["lift"] = None
                continue
            fim_efetivo = min(agora, fim_c) if fim_c else agora
            dur = max(1, min((fim_efetivo - ini).days or 1, 60))
            fim_dur = ini + timedelta(days=dur)
            antes_ini = ini - timedelta(days=dur)
            un_dur = un_antes = 0
            for iid in itens_c:
                for (dt, qtd) in por_item_datas.get(iid, []):
                    if ini <= dt < fim_dur:
                        un_dur += qtd
                    elif antes_ini <= dt < ini:
                        un_antes += qtd
            if un_dur == 0 and un_antes == 0:
                cf["lift"] = {"status": "sem_dados", "dur_dias": dur, "un_durante": 0, "un_antes": 0, "pct": None}
            elif un_antes == 0:
                cf["lift"] = {"status": "novo_giro", "pct": None, "un_durante": un_dur, "un_antes": 0, "dur_dias": dur}
            else:
                pct = round((un_dur / un_antes - 1) * 100, 1)
                st = "acelerou" if pct >= 15 else ("revisar" if pct <= -15 else "neutra")
                cf["lift"] = {"status": st, "pct": pct, "un_durante": un_dur, "un_antes": un_antes, "dur_dias": dur}
    else:
        for cf in camp_fin.values():
            cf["lift"] = None

    campanhas = sorted(camp_fin.values(), key=lambda c: c["voce_recebe"], reverse=True)
    for c in campanhas:
        c["voce_recebe"] = round(c["voce_recebe"], 2)
        c["desconto"] = round(c["desconto"], 2)
        c.pop("itens", None)
    totais = {"voce_recebe": round(sum(c["voce_recebe"] for c in campanhas), 2),
              "desconto": round(sum(c["desconto"] for c in campanhas), 2),
              "produtos": len(produtos), "campanhas": len(campanhas),
              "vendas_30d": sum(p["vendas_30d"] for p in produtos),
              "receita_30d": round(sum(p["receita_30d"] for p in produtos), 2),
              "cache_pedidos": tem_pedidos,
              "lift": {
                  "aceleraram": sum(1 for c in campanhas if (c.get("lift") or {}).get("status") == "acelerou"),
                  "neutras": sum(1 for c in campanhas if (c.get("lift") or {}).get("status") == "neutra"),
                  "revisar": sum(1 for c in campanhas if (c.get("lift") or {}).get("status") == "revisar"),
                  "novo_giro": sum(1 for c in campanhas if (c.get("lift") or {}).get("status") == "novo_giro"),
              }}
    resultado = {"total_produtos": len(produtos), "promocoes_varridas": min(len(promos), 30),
                 "produtos": produtos, "campanhas": campanhas, "totais": totais,
                 "janela": rotulo_janela,
                 "atualizado_em": datetime.utcnow().isoformat(), "cache": False}
    _PART_CACHE[chave] = {"ts": datetime.utcnow(), "data": resultado}
    return resultado


@app.get("/api/mercadolivre/ads/painel")
def ml_ads_painel(dias: int = Query(30), user: User = Depends(auth.get_current_user)):
    """Product Ads real: verifica se o Advertising está habilitado; se sim, traz campanhas + métricas.
    Sem dados fabricados — 403/404 vira estado 'não habilitado'."""
    from . import mercadolivre as ml
    try:
        adv = ml.ads_advertisers(user.id)
    except ml.MLNaoConfigurado as e:
        raise HTTPException(status_code=400, detail=f"Mercado Livre não conectado: {e}")
    except ml.MLErro as e:
        return {"habilitado": False,
                "motivo": "Product Ads (permissão Advertising) não está habilitado nesta conta.",
                "detalhe": str(e)[:200]}
    lista = (adv.get("advertisers") if isinstance(adv, dict) else adv) or []
    if not lista:
        return {"habilitado": False, "motivo": "Nenhum advertiser de Product Ads encontrado nesta conta."}
    a0 = lista[0]
    advertiser_id = a0.get("advertiser_id") or a0.get("id")
    site_id = a0.get("site_id") or "MLB"
    conta = a0.get("account_name") or a0.get("nickname")
    df = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%d")
    dt = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        camp = ml.ads_campanhas(advertiser_id, site_id, user.id, date_from=df, date_to=dt)
    except ml.MLErro as e:
        return {"habilitado": True, "advertiser_id": advertiser_id, "site_id": site_id, "conta": conta,
                "erro_campanhas": str(e)[:200], "campanhas": [], "totais": {}}
    results = (camp.get("results") if isinstance(camp, dict) else camp) or []
    campanhas = []
    tot = {"cost": 0.0, "amount": 0.0, "clicks": 0, "prints": 0}
    for c in results:
        mtr = c.get("metrics") or {}
        campanhas.append({
            "id": c.get("id") or c.get("campaign_id"), "nome": c.get("name"),
            "status": c.get("status"), "budget": _npx(c.get("budget")),
            "acos_target": _npx(c.get("acos_target")), "strategy": c.get("strategy"),
            "clicks": mtr.get("clicks"), "prints": mtr.get("prints"), "ctr": _npx(mtr.get("ctr")),
            "cost": _npx(mtr.get("cost")), "cpc": _npx(mtr.get("cpc")), "acos": _npx(mtr.get("acos")),
            "roas": _npx(mtr.get("roas")), "cvr": _npx(mtr.get("cvr")),
            "gmv": _npx(mtr.get("total_amount")),
        })
        tot["cost"] += mtr.get("cost") or 0
        tot["amount"] += mtr.get("total_amount") or 0
        tot["clicks"] += mtr.get("clicks") or 0
        tot["prints"] += mtr.get("prints") or 0
    campanhas.sort(key=lambda x: x.get("cost") or 0, reverse=True)
    roas = round(tot["amount"] / tot["cost"], 2) if tot["cost"] else None
    acos = round(tot["cost"] / tot["amount"] * 100, 1) if tot["amount"] else None
    return {"habilitado": True, "advertiser_id": advertiser_id, "site_id": site_id, "conta": conta,
            "periodo_dias": dias, "campanhas": campanhas,
            "totais": {"gasto": round(tot["cost"], 2), "gmv": round(tot["amount"], 2),
                       "clicks": tot["clicks"], "prints": tot["prints"], "roas": roas, "acos": acos}}


def _ads_advertiser(user_id):
    """Resolve (advertiser_id, site_id) do primeiro advertiser de Product Ads."""
    from . import mercadolivre as ml
    adv = ml.ads_advertisers(user_id)
    lista = (adv.get("advertisers") if isinstance(adv, dict) else adv) or []
    if not lista:
        raise HTTPException(status_code=400, detail="Nenhum advertiser de Product Ads nesta conta.")
    a0 = lista[0]
    return (a0.get("advertiser_id") or a0.get("id"), a0.get("site_id") or "MLB")


@app.get("/api/mercadolivre/ads/campanha/{campaign_id}/itens")
def ml_ads_campanha_itens(campaign_id: str, dias: int = Query(30),
                          user: User = Depends(auth.get_current_user)):
    """Anúncios dentro de uma campanha, com métricas por item (best-effort)."""
    from . import mercadolivre as ml
    try:
        _adv, site_id = _ads_advertiser(user.id)
    except ml.MLNaoConfigurado as e:
        raise HTTPException(status_code=400, detail=f"Mercado Livre não conectado: {e}")
    df = (datetime.utcnow() - timedelta(days=dias)).strftime("%Y-%m-%d")
    dt = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        raw = ml.ads_itens_campanha(campaign_id, site_id, user.id, date_from=df, date_to=dt)
    except ml.MLErro as e:
        return {"itens": [], "indisponivel": True, "detalhe": str(e)[:200]}
    results = (raw.get("results") if isinstance(raw, dict) else raw) or []
    itens = []
    from .models import MLItemCache
    ids = [str(c.get("id") or c.get("item_id")) for c in results if (c.get("id") or c.get("item_id"))]
    db = SessionLocal()
    try:
        cache = ({m.item_id: m for m in db.query(MLItemCache).filter(
                  MLItemCache.user_id == user.id, MLItemCache.item_id.in_(ids)).all()} if ids else {})
    finally:
        db.close()
    for c in results:
        iid = str(c.get("id") or c.get("item_id") or "")
        mtr = c.get("metrics") or {}
        m = cache.get(iid)
        itens.append({
            "item_id": iid, "titulo": (m.titulo if m else c.get("title")),
            "imagem": (m.imagem if m else c.get("thumbnail")), "status": c.get("status"),
            "clicks": mtr.get("clicks"), "prints": mtr.get("prints"), "ctr": _npx(mtr.get("ctr")),
            "cost": _npx(mtr.get("cost")), "cpc": _npx(mtr.get("cpc")), "acos": _npx(mtr.get("acos")),
            "roas": _npx(mtr.get("roas")), "cvr": _npx(mtr.get("cvr")), "gmv": _npx(mtr.get("total_amount")),
        })
    itens.sort(key=lambda x: x.get("cost") or 0, reverse=True)
    return {"campaign_id": campaign_id, "itens": itens}


@app.post("/api/mercadolivre/ads/campanha/{campaign_id}")
def ml_ads_campanha_editar(campaign_id: str, payload: dict = Body(...),
                           user: User = Depends(auth.get_current_user)):
    """Pausa/ativa ou ajusta budget/ACOS-alvo da campanha (best-effort — PUT campaigns/{id})."""
    from . import mercadolivre as ml
    campos = {}
    if payload.get("status") in ("active", "paused"):
        campos["status"] = payload["status"]
    if payload.get("budget") is not None:
        try:
            campos["budget"] = round(float(payload["budget"]), 2)
        except (TypeError, ValueError):
            pass
    if payload.get("acos_target") is not None:
        try:
            campos["acos_target"] = float(payload["acos_target"])
        except (TypeError, ValueError):
            pass
    if not campos:
        raise HTTPException(status_code=422, detail="Nada para editar (status, budget ou acos_target).")
    try:
        _adv, site_id = _ads_advertiser(user.id)
        r = ml.ads_editar_campanha(campaign_id, site_id, campos, user.id)
    except ml.MLNaoConfigurado as e:
        raise HTTPException(status_code=400, detail=f"Mercado Livre não conectado: {e}")
    except ml.MLErro as e:
        raise HTTPException(status_code=502, detail=f"Mercado Ads recusou a alteração: {str(e)[:200]}")
    return {"ok": True, "campaign_id": campaign_id, "aplicado": campos, "retorno": r}


@app.get("/api/mercadolivre/promocoes/promocao/{promotion_id}/metricas")
def ml_promo_promocao_metricas(promotion_id: str, promotion_type: str = Query(...),
                               inicio: str = Query(None), fim: str = Query(None),
                               user: User = Depends(auth.get_current_user)):
    """Métricas reais de uma campanha: agrega net_proceeds dos itens (líquido real do ML),
    cruza com custo/Preço Bling e gera insights sugestivos (o que cresce, o que preocupa, o que fazer)."""
    raw = _ml_run(lambda ml: ml.itens_promocao(promotion_id, promotion_type, user_id=user.id))
    lista = raw.get("results") if isinstance(raw, dict) else raw
    lista = lista or []
    base = []
    for it in lista:
        iid = it.get("id") or it.get("item_id")
        if not iid:
            continue
        np_ = it.get("net_proceeds") if isinstance(it.get("net_proceeds"), dict) else {}
        base.append({
            "item_id": iid, "price": _npx(it.get("price")),
            "original_price": _npx(it.get("original_price")) or _npx(it.get("price")),
            "net": _npx(np_.get("amount")), "suggested": _npx(it.get("suggested_discounted_price")),
            "status": it.get("status"),
        })
    ids = [b["item_id"] for b in base]
    from .models import MLItemCache, ProdutoCache
    db = SessionLocal()
    try:
        mlc = ({m.item_id: m for m in db.query(MLItemCache).filter(
                MLItemCache.user_id == user.id, MLItemCache.item_id.in_(ids)).all()} if ids else {})
    finally:
        db.close()
    faltando = [i for i in ids if i not in mlc or not mlc[i].titulo]
    extra_map = {}
    if faltando:
        extra = _ml_run(lambda ml: ml.obter_itens(
            faltando[:50], user.id,
            attributes="id,title,price,thumbnail,pictures,seller_custom_field,attributes,"
                       "available_quantity,shipping,health,catalog_listing,catalog_product_id")) or []
        extra_map = {e["item_id"]: e for e in extra}
    skus = [m.sku for m in mlc.values() if m.sku] + [e.get("sku") for e in extra_map.values() if e.get("sku")]
    db = SessionLocal()
    try:
        prods = ({p.sku: p for p in db.query(ProdutoCache).filter(
                  ProdutoCache.user_id == user.id, ProdutoCache.sku.in_(skus)).all()} if skus else {})
    finally:
        db.close()
    cfg = precificacao.obter_config(user.id)
    det = []
    for b in base:
        m = mlc.get(b["item_id"])
        ex = extra_map.get(b["item_id"], {})
        sku_i = (m.sku if m else None) or ex.get("sku")
        prod = prods.get(sku_i) if sku_i else None
        preco_bling = float(prod.preco) if (prod and prod.preco and prod.preco > 0) else None
        custo = float(prod.custo) if (prod and prod.custo and prod.custo > 0) else None
        piso = (precificacao.preco_minimo_para_liquido(cfg, "mercadolivre", preco_bling)
                if preco_bling is not None else None)
        price = b["price"] or b["original_price"]
        orig = b["original_price"]
        desc = round((1 - price / orig) * 100, 1) if (orig and price and orig > 0) else None
        liquido = b["net"]
        lucro = round(liquido - custo, 2) if (liquido is not None and custo is not None) else None
        margem = round(lucro / price * 100, 1) if (lucro is not None and price and price > 0) else None
        abaixo = (liquido is not None and preco_bling is not None and liquido < preco_bling)
        estoque = m.estoque if m else None
        oport = (b["suggested"] is not None and price is not None and b["suggested"] < price - 0.005
                 and (piso is None or b["suggested"] >= piso))
        det.append({
            "item_id": b["item_id"],
            "titulo": (m.titulo if m else None) or ex.get("titulo"),
            "sku": sku_i,
            "imagem": (m.imagem if m else None) or ex.get("imagem"),
            "preco_catalogo": (float(m.preco) if (m and m.preco) else ex.get("preco")),
            "logistic_type": (m.logistic_type if m else None) or ex.get("logistic_type"),
            "frete_gratis": ex.get("frete_gratis"),
            "catalogo": ex.get("catalogo"),
            "saude_item": (float(m.saude) if (m and m.saude is not None) else ex.get("health")),
            "em_promocao": bool(m.em_promocao) if m else None,
            "price": price, "original_price": orig, "desconto_pct": desc,
            "liquido": liquido, "custo": custo, "lucro": lucro, "margem_pct": margem,
            "preco_bling": preco_bling, "abaixo_piso": bool(abaixo), "suggested": b["suggested"],
            "oportunidade": bool(oport), "estoque": (estoque if estoque is not None else ex.get("estoque")),
            "status": b["status"],
        })

    def _avg(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 1) if xs else None

    n = len(det)
    liquidos = [d["liquido"] for d in det if d["liquido"] is not None]
    lucros = [d["lucro"] for d in det if d["lucro"] is not None]
    abaixo_piso = sum(1 for d in det if d["abaixo_piso"])
    oportunidades = sum(1 for d in det if d["oportunidade"])
    estoque_baixo = sum(1 for d in det if (d["estoque"] is not None and 0 < d["estoque"] <= 5))
    sem_custo = sum(1 for d in det if d["custo"] is None)
    com_custo = n - sem_custo
    ativos = sum(1 for d in det if (d["status"] in (None, "active", "started", "enabled")))
    liquido_total = round(sum(liquidos), 2) if liquidos else None
    lucro_total = round(sum(lucros), 2) if lucros else None
    desconto_medio = _avg([d["desconto_pct"] for d in det])
    margem_media = _avg([d["margem_pct"] for d in det])
    preco_promo_total = round(sum(d["price"] for d in det if d["price"]), 2) if det else None
    preco_orig_total = round(sum(d["original_price"] for d in det if d["original_price"]), 2) if det else None

    dias_total = dias_dec = dias_rest = pct_tempo = None
    try:
        from datetime import datetime as _dt

        def _pd(s):
            if not s:
                return None
            return _dt.fromisoformat(str(s).replace("Z", "")[:19])
        di, dfim, hoje = _pd(inicio), _pd(fim), _dt.utcnow()
        if di and dfim and dfim > di:
            dias_total = (dfim - di).days or 1
            dias_dec = max(0, (hoje - di).days)
            dias_rest = max(0, (dfim - hoje).days)
            pct_tempo = min(100, max(0, round((hoje - di).total_seconds() / (dfim - di).total_seconds() * 100)))
    except Exception:  # noqa: BLE001
        pass

    if abaixo_piso > 0 or estoque_baixo > 0:
        saude = "risco"
    elif (desconto_medio is not None and desconto_medio > 40) or oportunidades > 0 or n == 0:
        saude = "atencao"
    else:
        saude = "saudavel"

    ins = []

    def add(t, ic, ti, de):
        ins.append({"tipo": t, "icone": ic, "titulo": ti, "detalhe": de})

    if n == 0:
        add("info", "PackageOpen", "Campanha ainda sem itens", "Use 'Adicionar itens' para incluir anúncios nesta campanha.")
    if abaixo_piso > 0:
        add("risco", "ShieldAlert", f"{abaixo_piso} {'item' if abaixo_piso == 1 else 'itens'} furando a margem",
            "O líquido estimado (net do ML) está abaixo do Preço Bling. Suba o preço desses itens ou remova-os da campanha.")
    if abaixo_piso == 0 and margem_media is not None and margem_media >= 15 and n > 0:
        add("positivo", "ShieldCheck", f"Margem saudável — média de {margem_media}%",
            "Nenhum item fura o piso e a margem está equilibrada. Bom candidato a manter ou renovar.")
    if oportunidades > 0:
        add("oportunidade", "TrendingUp", f"{oportunidades} {'item' if oportunidades == 1 else 'itens'} com folga para descontar mais",
            "O ML sugere um desconto maior que ainda fica acima do piso. Aprofundar pode aumentar conversão e competitividade.")
    if desconto_medio is not None and desconto_medio < 8 and n > 0:
        add("acao", "Percent", f"Desconto médio baixo ({desconto_medio}%)",
            "Pode ter pouco apelo para o comprador. Considere aprofundar onde há folga de margem.")
    if desconto_medio is not None and desconto_medio > 40:
        add("risco", "Flame", f"Desconto médio alto ({desconto_medio}%)",
            "Ótimo para girar, mas confirme se a margem se mantém — priorize corrigir itens abaixo do piso.")
    if estoque_baixo > 0:
        add("risco", "PackageX", f"{estoque_baixo} {'item' if estoque_baixo == 1 else 'itens'} com estoque baixo",
            "Risco de ruptura no meio da campanha. Reabasteça ou reduza a exposição desses itens.")
    if dias_rest is not None and dias_rest <= 2:
        add("acao", "CalendarClock", f"Termina em {dias_rest} {'dia' if dias_rest == 1 else 'dias'}",
            "Programe a próxima janela para a loja não ficar sem desconto ativo (auto-continuar).")
    if sem_custo > 0:
        add("info", "HelpCircle", f"{sem_custo} {'item' if sem_custo == 1 else 'itens'} sem custo no Bling",
            "Cadastre o custo para calcular o lucro real desses itens e liberar a leitura de margem.")

    # --- vendas dos itens na janela (do cache de pedidos) ---
    vendas = None
    if inicio and fim:
        _ini = _parse_ml_dt(inicio); _fim = _parse_ml_dt(fim); _agora = datetime.utcnow()
        if _ini and _fim:
            _fime = min(_fim, _agora)
            if _fime > _ini:
                _dur = _fime - _ini
                _ids = [d["item_id"] for d in det if d["item_id"]]
                _at = _vendas_itens(user.id, _ids, _ini, _fime)
                _bl = _vendas_itens(user.id, _ids, _ini - _dur, _ini)
                from .models import MLPedidoItemCache as _MPC
                _db = SessionLocal()
                try:
                    _tem = _db.query(_MPC).filter(_MPC.user_id == user.id).first() is not None
                finally:
                    _db.close()

                def _delta(a, b):
                    return round((a - b) / b * 100, 1) if (b and b > 0) else None
                vendas = {
                    "unidades": _at["unidades"], "receita": _at["receita"], "serie": _at["serie"],
                    "unidades_baseline": _bl["unidades"], "receita_baseline": _bl["receita"],
                    "delta_unidades_pct": _delta(_at["unidades"], _bl["unidades"]),
                    "delta_receita_pct": _delta(_at["receita"], _bl["receita"]),
                    "baseline_disponivel": _bl["unidades"] > 0, "cache_vazio": not _tem,
                    "janela_ini": _ini.isoformat(), "janela_fim": _fime.isoformat(),
                }
    if vendas:
        if vendas["cache_vazio"]:
            add("info", "RefreshCw", "Sincronize os pedidos para ver vendas",
                "O cache de pedidos está vazio. Rode a sincronização para liberar a análise de vendas desta campanha.")
        elif vendas["delta_unidades_pct"] is not None and vendas["delta_unidades_pct"] >= 10:
            add("positivo", "TrendingUp", f"Vendas +{vendas['delta_unidades_pct']}% na janela",
                "Os itens desta campanha venderam mais que no período anterior equivalente — a promoção está impulsionando as vendas.")
        elif vendas["delta_unidades_pct"] is not None and vendas["delta_unidades_pct"] <= -10:
            add("risco", "TrendingDown", f"Vendas {vendas['delta_unidades_pct']}% na janela",
                "Queda de vendas dos itens vs o período anterior. Avalie aprofundar o desconto (respeitando o piso) ou revisar preço e concorrência.")
        elif vendas["unidades"] > 0 and not vendas["baseline_disponivel"]:
            add("info", "Info", "Sem base para comparar vendas",
                "Há vendas na janela, mas não há histórico anterior suficiente. Amplie a sincronização para comparar.")

    # --- projeção: no ritmo de venda dos últimos 30 dias, quanto renderia na duração da campanha ---
    projecao = None
    if det:
        _idsp = [d["item_id"] for d in det if d["item_id"]]
        _look = 30
        _hist = _vendas_itens(user.id, _idsp, datetime.utcnow() - timedelta(days=_look), datetime.utcnow())
        _diasc = dias_total or 7
        _rate = _hist["unidades"] / _look
        _unest = round(_rate * _diasc)
        _pmed = round(preco_promo_total / len(det), 2) if (preco_promo_total and det) else None
        _recest = round(_unest * _pmed, 2) if (_pmed and _unest) else (0.0 if _pmed else None)
        projecao = {
            "base_dias": _look, "base_unidades": _hist["unidades"], "rate_dia": round(_rate, 2),
            "dias_campanha": _diasc, "unidades_estimadas": _unest, "receita_estimada": _recest,
            "preco_medio": _pmed, "cache_vazio": (vendas or {}).get("cache_vazio", _hist["unidades"] == 0),
        }

    return {
        "promotion_id": promotion_id, "promotion_type": promotion_type,
        "itens": n, "itens_ativos": ativos, "com_custo": com_custo, "sem_custo": sem_custo,
        "desconto_medio_pct": desconto_medio, "liquido_total": liquido_total, "lucro_total": lucro_total,
        "margem_media_pct": margem_media, "abaixo_piso": abaixo_piso, "oportunidades": oportunidades,
        "estoque_baixo": estoque_baixo, "preco_promo_total": preco_promo_total, "preco_original_total": preco_orig_total,
        "dias_total": dias_total, "dias_decorridos": dias_dec, "dias_restantes": dias_rest, "pct_tempo": pct_tempo,
        "saude": saude, "insights": ins, "itens_detalhe": det, "vendas": vendas, "projecao": projecao,
    }


# =========================== Cache de pedidos (vendas) ===========================
def _parse_ml_dt(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:  # noqa: BLE001
        try:
            return datetime.fromisoformat(str(s).replace("Z", "")[:19])
        except Exception:  # noqa: BLE001
            return None


def _upsert_pedido_cache(db, user_id, o) -> bool:
    """Upsert de um pedido do ML nas duas tabelas de cache. Não faz commit."""
    from .models import MLPedidoCache, MLPedidoItemCache
    oid = str(o.get("id") or "")
    if not oid:
        return False
    dc = _parse_ml_dt(o.get("date_created"))
    itens_min, unidades = [], 0
    for it in (o.get("order_items") or []):
        item = it.get("item") or {}
        iid = str(item.get("id") or "") or None
        sku = str(item.get("seller_sku") or "") or None
        qty = int(it.get("quantity") or 0)
        unit = float(it.get("unit_price") or 0)
        fee = float(it.get("sale_fee") or 0)
        unidades += qty
        itens_min.append({"item_id": iid, "sku": sku, "titulo": item.get("title"),
                          "quantidade": qty, "unit_price": round(unit, 2), "sale_fee": round(fee, 2)})
    campos = dict(pack_id=(str(o.get("pack_id")) if o.get("pack_id") else None),
                  status=o.get("status"), date_created=dc, date_closed=_parse_ml_dt(o.get("date_closed")),
                  total_amount=float(o.get("total_amount") or 0), paid_amount=float(o.get("paid_amount") or 0),
                  currency_id=o.get("currency_id"), unidades=unidades, itens=itens_min,
                  atualizado_em=datetime.utcnow())
    ped = db.query(MLPedidoCache).filter(MLPedidoCache.user_id == user_id, MLPedidoCache.order_id == oid).first()
    if ped:
        for k, v in campos.items():
            setattr(ped, k, v)
    else:
        db.add(MLPedidoCache(user_id=user_id, order_id=oid, **campos))
    db.query(MLPedidoItemCache).filter(MLPedidoItemCache.user_id == user_id,
                                       MLPedidoItemCache.order_id == oid).delete()
    for m in itens_min:
        db.add(MLPedidoItemCache(user_id=user_id, order_id=oid, item_id=m["item_id"], sku=m["sku"],
                                 titulo=m["titulo"], quantidade=m["quantidade"], unit_price=m["unit_price"],
                                 receita=round(m["unit_price"] * m["quantidade"], 2), sale_fee=m["sale_fee"],
                                 status=o.get("status"), date_created=dc))
    return True


def _vendas_itens(user_id, item_ids, ini, fim):
    """Soma unidades/receita dos itens (do cache) no período [ini, fim) + série diária. Exclui cancelados."""
    from .models import MLPedidoItemCache
    if not item_ids or not ini or not fim:
        return {"unidades": 0, "receita": 0.0, "serie": []}
    db = SessionLocal()
    try:
        rows = db.query(MLPedidoItemCache).filter(
            MLPedidoItemCache.user_id == user_id,
            MLPedidoItemCache.item_id.in_(list(item_ids)),
            MLPedidoItemCache.date_created >= ini,
            MLPedidoItemCache.date_created < fim,
        ).all()
    finally:
        db.close()
    unid, rec, serie = 0, 0.0, {}
    for r in rows:
        if (r.status or "") == "cancelled":
            continue
        unid += (r.quantidade or 0)
        rec += (r.receita or 0)
        d = r.date_created.date().isoformat() if r.date_created else "?"
        s = serie.setdefault(d, {"unidades": 0, "receita": 0.0})
        s["unidades"] += (r.quantidade or 0)
        s["receita"] += (r.receita or 0)
    serie_list = [{"dia": k, "unidades": v["unidades"], "receita": round(v["receita"], 2)}
                  for k, v in sorted(serie.items())]
    return {"unidades": unid, "receita": round(rec, 2), "serie": serie_list}


@app.post("/api/mercadolivre/pedidos/cache/backfill")
def ml_pedidos_backfill(dias: int = Query(90, ge=1, le=365), user: User = Depends(auth.get_current_user)):
    """Busca os pedidos dos últimos `dias` no ML e popula o cache de vendas (upsert idempotente)."""
    from datetime import timedelta
    ate = datetime.utcnow()
    desde = ate - timedelta(days=dias)
    desde_s = desde.strftime("%Y-%m-%dT00:00:00.000-00:00")
    ate_s = ate.strftime("%Y-%m-%dT23:59:59.000-00:00")
    MAX_PAG = 60
    gravados = paginas = 0
    truncado = False
    db = SessionLocal()
    try:
        for pag in range(MAX_PAG):
            raw = _ml_run(lambda ml, _p=pag: ml.listar_pedidos(user.id, None, desde_s, ate_s, _p * 50, 50)) or {}
            pagina = raw.get("results") or []
            paginas += 1
            for o in pagina:
                try:
                    if _upsert_pedido_cache(db, user.id, o):
                        gravados += 1
                except Exception:  # noqa: BLE001
                    db.rollback()
            try:
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
            if len(pagina) < 50:
                break
            if pag == MAX_PAG - 1:
                truncado = True
    finally:
        db.close()
    return {"ok": True, "dias": dias, "paginas": paginas, "pedidos_gravados": gravados, "truncado": truncado}


@app.get("/api/mercadolivre/pedidos/cache/status")
def ml_pedidos_cache_status(user: User = Depends(auth.get_current_user)):
    from .models import MLPedidoCache
    from sqlalchemy import func
    db = SessionLocal()
    try:
        total = db.query(MLPedidoCache).filter(MLPedidoCache.user_id == user.id).count()
        primeiro = ultimo = None
        if total:
            primeiro = db.query(func.min(MLPedidoCache.date_created)).filter(MLPedidoCache.user_id == user.id).scalar()
            ultimo = db.query(func.max(MLPedidoCache.date_created)).filter(MLPedidoCache.user_id == user.id).scalar()
    finally:
        db.close()
    return {"pedidos": total, "vazio": total == 0,
            "primeiro": primeiro.isoformat() if primeiro else None,
            "ultimo": ultimo.isoformat() if ultimo else None}


@app.get("/api/mercadolivre/pedidos/parados")
def ml_pedidos_parados(dias: int = Query(30, ge=1, le=365), limit: int = Query(20, ge=1, le=60),
                       user: User = Depends(auth.get_current_user)):
    """Produtos ativos SEM venda nos últimos `dias` (do cache), com folga de margem — candidatos a promoção."""
    from .models import MLItemCache, MLPedidoItemCache, ProdutoCache
    from sqlalchemy import func
    corte = datetime.utcnow() - timedelta(days=dias)
    db = SessionLocal()
    try:
        ultimas = dict(db.query(MLPedidoItemCache.item_id, func.max(MLPedidoItemCache.date_created))
                       .filter(MLPedidoItemCache.user_id == user.id, MLPedidoItemCache.item_id.isnot(None))
                       .group_by(MLPedidoItemCache.item_id).all())
        itens = db.query(MLItemCache).filter(MLItemCache.user_id == user.id, MLItemCache.status == "active").all()
        skus = [i.sku for i in itens if i.sku]
        prods = ({p.sku: p for p in db.query(ProdutoCache).filter(
                  ProdutoCache.user_id == user.id, ProdutoCache.sku.in_(skus)).all()} if skus else {})
        tem_cache = db.query(MLPedidoItemCache).filter(MLPedidoItemCache.user_id == user.id).first() is not None
    finally:
        db.close()
    cfg = precificacao.obter_config(user.id)
    parados = []
    agora = datetime.utcnow()
    for it in itens:
        ult = ultimas.get(it.item_id)
        if ult and ult >= corte:
            continue  # vendeu no período — não é parado
        dias_sem = (agora - ult).days if ult else None  # None = sem venda registrada no cache
        prod = prods.get(it.sku) if it.sku else None
        preco_bling = float(prod.preco) if (prod and prod.preco and prod.preco > 0) else None
        piso = (precificacao.preco_minimo_para_liquido(cfg, "mercadolivre", preco_bling)
                if preco_bling is not None else None)
        preco = float(it.preco or 0)
        folga = round((1 - piso / preco) * 100, 1) if (piso and preco > 0 and piso < preco) else 0.0
        parados.append({
            "item_id": it.item_id, "titulo": it.titulo, "sku": it.sku, "imagem": it.imagem,
            "preco": preco, "estoque": it.estoque, "preco_bling": preco_bling, "piso": piso,
            "dias_sem_venda": dias_sem, "folga_desconto_pct": folga,
        })
    # promováveis (folga>0) primeiro; depois mais parados (nunca vendeu = topo); depois maior folga
    parados.sort(key=lambda x: (x["folga_desconto_pct"] > 0,
                                x["dias_sem_venda"] if x["dias_sem_venda"] is not None else 99999,
                                x["folga_desconto_pct"]), reverse=True)
    return {"dias": dias, "cache_vazio": not tem_cache, "total": len(parados), "itens": parados[:limit]}


@app.get("/api/mercadolivre/buybox")
def ml_buybox(limit: int = Query(20, ge=1, le=40), offset: int = Query(0),
              somente_perdendo: bool = Query(True), user: User = Depends(auth.get_current_user)):
    """Rastreio de buybox: para uma página de anúncios ativos, consulta price_to_win, normaliza
    ganhando/perdendo/compartilhando e cruza com o piso — sugere o preço que recupera o topo sem furar a margem."""
    from .models import MLItemCache, ProdutoCache
    from types import SimpleNamespace
    db = SessionLocal()
    try:
        base_q = db.query(MLItemCache).filter(MLItemCache.user_id == user.id, MLItemCache.status == "active")
        total = base_q.count()
        itens = [SimpleNamespace(item_id=i.item_id, sku=i.sku, preco=i.preco, titulo=i.titulo,
                                 imagem=i.imagem, estoque=i.estoque)
                 for i in base_q.order_by(MLItemCache.preco.desc()).offset(offset).limit(limit).all()]
    finally:
        db.close()
    if total == 0:  # cache de anúncios vazio → busca ativos direto do ML
        ids_ativos = _ml_run(lambda ml: ml.listar_ids(user.id, filtros={"status": "active"},
                                                       limite=offset + limit + 10)) or []
        total = len(ids_ativos)
        page_ids = ids_ativos[offset:offset + limit]
        det = (_ml_run(lambda ml: ml.obter_itens(
            page_ids, user.id,
            attributes="id,title,price,thumbnail,pictures,seller_custom_field,attributes,available_quantity,catalog_listing,catalog_product_id"))
            if page_ids else [])
        itens = [SimpleNamespace(item_id=d.get("item_id"), sku=d.get("sku"), preco=d.get("preco"),
                                 titulo=d.get("titulo"), imagem=d.get("imagem"), estoque=d.get("estoque"))
                 for d in det]
    skus = [i.sku for i in itens if i.sku]
    db = SessionLocal()
    try:
        prods = ({p.sku: p for p in db.query(ProdutoCache).filter(
                  ProdutoCache.user_id == user.id, ProdutoCache.sku.in_(skus)).all()} if skus else {})
    finally:
        db.close()
    cfg = precificacao.obter_config(user.id)

    def _run(ml):
        out = []
        resumo = {"ganhando": 0, "perdendo": 0, "compartilhando": 0, "fora": 0,
                  "sem_catalogo": 0, "recuperavel": 0}
        for it in itens:
            try:
                ptw = ml.preco_para_ganhar(it.item_id, user.id) or {}
            except Exception:  # noqa: BLE001
                # price_to_win só existe para anúncios de CATÁLOGO — comuns caem aqui
                resumo["sem_catalogo"] += 1
                continue
            status_ml = (ptw.get("status") or "").lower()
            p2w = _npx(ptw.get("price_to_win"))
            winner = ptw.get("winner") if isinstance(ptw.get("winner"), dict) else {}
            pv = _npx(winner.get("price"))
            if status_ml == "winning":
                st = "ganhando"
            elif "sharing" in status_ml:
                st = "compartilhando"
            elif status_ml in ("competing", "not_winning", "listed") or p2w is not None:
                st = "perdendo"
            else:
                st = "fora"
            if st == "fora":
                resumo["fora"] += 1
                continue
            resumo[st] += 1
            preco = float(it.preco or 0)
            prod = prods.get(it.sku) if it.sku else None
            preco_bling = float(prod.preco) if (prod and prod.preco and prod.preco > 0) else None
            piso = (precificacao.preco_minimo_para_liquido(cfg, "mercadolivre", preco_bling)
                    if preco_bling is not None else None)
            recuperavel = (st in ("perdendo", "compartilhando") and p2w is not None
                           and (piso is None or p2w >= piso))
            if recuperavel:
                resumo["recuperavel"] += 1
            if somente_perdendo and st == "ganhando":
                continue
            out.append({
                "item_id": it.item_id, "titulo": it.titulo, "sku": it.sku, "imagem": it.imagem, "estoque": it.estoque,
                "status": st, "meu_preco": preco, "preco_para_ganhar": p2w, "preco_vencedor": pv,
                "preco_bling": preco_bling, "piso": piso, "recuperavel": bool(recuperavel),
                "diferenca": round(preco - p2w, 2) if (p2w is not None) else None,
            })
        return {"itens": out, "resumo": resumo}

    res = _ml_run(lambda ml: _run(ml))
    return {"total_catalogo": total, "offset": offset, "limit": limit, "verificados": len(itens),
            "itens": res["itens"], "resumo": res["resumo"], "tem_mais": offset + limit < total}


@app.post("/api/mercadolivre/buybox/ajustar-preco")
def ml_buybox_ajustar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Ajusta o preço padrão do anúncio (ex.: para recuperar o buybox) — com trava de margem (nunca abaixo do piso)."""
    iid, preco = payload.get("item_id"), payload.get("preco")
    if not iid or preco is None:
        raise HTTPException(status_code=422, detail="Informe item_id e preco.")
    _checa_piso(user.id, iid, preco)
    return _ml_run(lambda ml: ml.atualizar_preco(iid, float(preco), user.id))


def _gerar_sugestoes(user_id, limit=40):
    """Motor de sugestões dos agentes (modo sugestivo): analisa catálogo + cache de pedidos + margem e
    devolve ações recomendadas por item (uma por item, a de maior prioridade), sempre acima do piso."""
    from .models import MLItemCache, MLPedidoItemCache, ProdutoCache
    from sqlalchemy import func
    agora = datetime.utcnow()
    j30, j60 = agora - timedelta(days=30), agora - timedelta(days=60)
    db = SessionLocal()
    try:
        itens = db.query(MLItemCache).filter(MLItemCache.user_id == user_id, MLItemCache.status == "active").all()
        skus = [i.sku for i in itens if i.sku]
        prods = ({p.sku: p for p in db.query(ProdutoCache).filter(
                  ProdutoCache.user_id == user_id, ProdutoCache.sku.in_(skus)).all()} if skus else {})
        rows = db.query(MLPedidoItemCache.item_id, MLPedidoItemCache.quantidade,
                        MLPedidoItemCache.date_created, MLPedidoItemCache.status).filter(
            MLPedidoItemCache.user_id == user_id, MLPedidoItemCache.date_created >= j60,
            MLPedidoItemCache.item_id.isnot(None)).all()
        ultima = dict(db.query(MLPedidoItemCache.item_id, func.max(MLPedidoItemCache.date_created)).filter(
            MLPedidoItemCache.user_id == user_id, MLPedidoItemCache.item_id.isnot(None))
            .group_by(MLPedidoItemCache.item_id).all())
        tem_cache = db.query(MLPedidoItemCache).filter(MLPedidoItemCache.user_id == user_id).first() is not None
    finally:
        db.close()
    recente, anterior = {}, {}
    for iid, qty, dt, st in rows:
        if (st or "") == "cancelled":
            continue
        alvo = recente if dt >= j30 else anterior
        alvo[iid] = alvo.get(iid, 0) + (qty or 0)
    cfg = precificacao.obter_config(user_id)
    sugs = []
    for it in itens:
        prod = prods.get(it.sku) if it.sku else None
        preco_bling = float(prod.preco) if (prod and prod.preco and prod.preco > 0) else None
        piso = (precificacao.preco_minimo_para_liquido(cfg, "mercadolivre", preco_bling)
                if preco_bling is not None else None)
        preco = float(it.preco or 0)
        folga = round((1 - piso / preco) * 100, 1) if (piso and preco > 0 and piso < preco) else 0.0
        estoque = it.estoque or 0
        ult = ultima.get(it.item_id)
        dias_sem = (agora - ult).days if ult else None
        rec, ant = recente.get(it.item_id, 0), anterior.get(it.item_id, 0)
        cands = []
        # Agente ESTOQUE PARADO: sem venda há 30d+ e com folga
        if (dias_sem is None or dias_sem >= 30) and folga > 0 and estoque > 0:
            d = max(5, min(round(folga) - 3, 20))
            cands.append({"agente": "parado", "prioridade": 70 + min(estoque, 25) + (min(dias_sem, 120) // 4 if dias_sem else 15),
                          "motivo": (f"{dias_sem}d sem vender" if dias_sem else "sem venda registrada") + f" · {estoque} un paradas",
                          "acao": f"desconto {d}%", "desconto_pct": d})
        # Agente GIRO: vendeu antes e caiu >40%
        if ant >= 2 and rec < ant * 0.6 and folga > 0:
            queda = round((1 - (rec / ant)) * 100)
            d = max(5, min(round(folga) - 3, 15))
            cands.append({"agente": "giro", "prioridade": 78 + min(ant, 25) + queda // 3,
                          "motivo": f"vendas caíram {queda}% (de {ant} p/ {rec} un em 30d)",
                          "acao": f"desconto {d}%", "desconto_pct": d})
        # Agente MARGEM: margem folgada e vendendo
        if folga >= 25 and rec >= 2:
            d = max(8, min(round(folga) - 10, 20))
            cands.append({"agente": "margem", "prioridade": 60 + round(folga) // 2 + min(rec, 20),
                          "motivo": f"margem folgada ({folga}%) e vende ({rec} un/30d) — desconto seguro p/ acelerar",
                          "acao": f"desconto {d}%", "desconto_pct": d})
        # Agente ESTOQUE BAIXO: pouco estoque e vendendo → urgência (Relâmpago)
        if 0 < estoque <= 5 and rec >= 2:
            cands.append({"agente": "estoque_baixo", "prioridade": 66 + min(rec, 20),
                          "motivo": f"estoque baixo ({estoque} un) e vendendo — Relâmpago com estoque reservado cria urgência",
                          "acao": "Relâmpago (estoque reservado)", "desconto_pct": None})
        if not cands:
            continue
        sug = max(cands, key=lambda c: c["prioridade"])
        dp = None
        if sug.get("desconto_pct"):
            dp = round(preco * (1 - sug["desconto_pct"] / 100), 2)
            if piso and dp < piso:
                dp = round(piso, 2)
        sug.update({"item_id": it.item_id, "titulo": it.titulo, "sku": it.sku, "imagem": it.imagem,
                    "preco": preco, "estoque": estoque, "preco_bling": preco_bling, "piso": piso,
                    "folga_pct": folga, "deal_price_sugerido": dp,
                    "capital": round(preco * estoque, 2), "vendas_30d": rec, "vendas_30_60d": ant})
        sugs.append(sug)
    sugs.sort(key=lambda x: x["prioridade"], reverse=True)
    por_agente = {}
    for s in sugs:
        por_agente[s["agente"]] = por_agente.get(s["agente"], 0) + 1
    capital_parado = round(sum(s["capital"] for s in sugs if s.get("agente") == "parado"), 2)
    resumo_impacto = {"capital_parado": capital_parado, "oportunidades": len(sugs),
                      "n_parado": por_agente.get("parado", 0), "n_giro": por_agente.get("giro", 0),
                      "n_margem": por_agente.get("margem", 0), "n_estoque_baixo": por_agente.get("estoque_baixo", 0)}
    return {"cache_vazio": not tem_cache, "total": len(sugs), "por_agente": por_agente,
            "resumo_impacto": resumo_impacto, "sugestoes": sugs[:limit]}


@app.get("/api/mercadolivre/agentes/sugestoes")
def ml_agentes_sugestoes(limit: int = Query(40, ge=1, le=120), user: User = Depends(auth.get_current_user)):
    return _gerar_sugestoes(user.id, limit)


def _rodar_agentes(user_id, gatilho="manual"):
    """Executa os agentes: aplica as sugestões SEGURAS (piso-safe) dos agentes ligados, com teto. Loga tudo."""
    from . import mercadolivre as ml
    from .models import AgenteConfig, AgenteExecucao
    db = SessionLocal()
    try:
        cfg = db.query(AgenteConfig).filter(AgenteConfig.user_id == user_id).first()
        if cfg and cfg.kill_switch:
            return {"aplicados": 0, "ignorados": 0, "falhas": 0, "candidatos": 0,
                    "bloqueado": "kill_switch", "detalhe": []}
        ligados = cfg.agentes if (cfg and cfg.agentes) else None
        teto = (cfg.max_por_execucao if (cfg and cfg.max_por_execucao) else 15)
        teto_desc = (cfg.teto_desconto_pct if cfg else None)
        if cfg and gatilho == "auto":  # marca cedo p/ evitar corrida entre workers
            cfg.ultima_execucao_auto = datetime.utcnow()
            db.add(cfg); db.commit()
    finally:
        db.close()

    dados = _gerar_sugestoes(user_id, limit=250)
    sugs = dados.get("sugestoes") or []

    def _ativo(k):
        return ligados is None or ligados.get(k) is not False

    aplicaveis = [s for s in sugs if s.get("deal_price_sugerido") is not None and _ativo(s.get("agente"))][:teto]
    aplicados = falhas = ignorados = 0
    detalhe = []
    fim_iso = (datetime.utcnow() + timedelta(days=7)).strftime("%Y-%m-%dT23:59:59.000-03:00")
    for s in aplicaveis:
        preco = s["deal_price_sugerido"]
        piso = s.get("piso")
        # teto de desconto: nunca aplica mais que o limite configurado (mas nunca abaixo do piso)
        if teto_desc and s.get("preco") and s.get("desconto_pct") and s["desconto_pct"] > teto_desc:
            preco_teto = round(s["preco"] * (1 - teto_desc / 100), 2)
            preco = max(preco, preco_teto)
            if piso is not None:
                preco = max(preco, round(piso, 2))
        base = {"item_id": s["item_id"], "titulo": s.get("titulo"), "agente": s.get("agente"),
                "desconto_pct": (round((1 - preco / s["preco"]) * 100, 1) if s.get("preco") else s.get("desconto_pct")),
                "preco": preco, "de": s.get("preco")}
        if piso is not None and preco < piso - 0.005:
            ignorados += 1
            detalhe.append({**base, "status": "ignorado_piso"})
            continue
        try:
            ml.criar_desconto_item(s["item_id"], preco, None, fim_iso, None, user_id)
            aplicados += 1
            detalhe.append({**base, "status": "aplicado"})
        except Exception as e:  # noqa: BLE001
            falhas += 1
            detalhe.append({**base, "status": "erro", "msg": str(e)[:120]})

    db = SessionLocal()
    try:
        db.add(AgenteExecucao(user_id=user_id, gatilho=gatilho, aplicados=aplicados,
                              ignorados=ignorados, falhas=falhas, detalhe=detalhe[:60]))
        db.commit()
    finally:
        db.close()
    return {"aplicados": aplicados, "ignorados": ignorados, "falhas": falhas,
            "candidatos": len(aplicaveis), "cache_vazio": dados.get("cache_vazio"), "detalhe": detalhe}


@app.post("/api/mercadolivre/agentes/rodar")
def ml_agentes_rodar(user: User = Depends(auth.get_current_user)):
    return _rodar_agentes(user.id, gatilho="manual")


@app.get("/api/mercadolivre/agentes/config")
def ml_agentes_config_get(user: User = Depends(auth.get_current_user)):
    from .models import AgenteConfig, AgenteExecucao
    db = SessionLocal()
    try:
        c = db.query(AgenteConfig).filter(AgenteConfig.user_id == user.id).first()
        ult = (db.query(AgenteExecucao).filter(AgenteExecucao.user_id == user.id)
               .order_by(AgenteExecucao.quando.desc()).first())
        base = {"automatico": False, "kill_switch": False, "agentes": None, "max_por_execucao": 15,
                "teto_desconto_pct": None, "intervalo_horas": 6, "ultima_execucao_auto": None}
        if c:
            base = {"automatico": c.automatico, "kill_switch": c.kill_switch, "agentes": c.agentes,
                    "max_por_execucao": c.max_por_execucao, "teto_desconto_pct": c.teto_desconto_pct,
                    "intervalo_horas": c.intervalo_horas,
                    "ultima_execucao_auto": c.ultima_execucao_auto.isoformat() if c.ultima_execucao_auto else None}
        base["ultima_execucao"] = ult.quando.isoformat() if (ult and ult.quando) else None
        return base
    finally:
        db.close()


@app.put("/api/mercadolivre/agentes/config")
def ml_agentes_config_put(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    from .models import AgenteConfig
    db = SessionLocal()
    try:
        c = db.query(AgenteConfig).filter(AgenteConfig.user_id == user.id).first()
        if not c:
            c = AgenteConfig(user_id=user.id); db.add(c)
        if "automatico" in payload:
            c.automatico = bool(payload["automatico"])
        if "kill_switch" in payload:
            c.kill_switch = bool(payload["kill_switch"])
        if "agentes" in payload:
            c.agentes = payload["agentes"]
        if "max_por_execucao" in payload:
            c.max_por_execucao = max(1, min(int(payload["max_por_execucao"]), 100))
        if "teto_desconto_pct" in payload:
            v = payload["teto_desconto_pct"]
            c.teto_desconto_pct = None if v in (None, "", 0) else max(1, min(int(v), 70))
        if "intervalo_horas" in payload:
            c.intervalo_horas = max(1, min(int(payload["intervalo_horas"]), 168))
        c.atualizado_em = datetime.utcnow()
        db.commit()
        return {"ok": True, "automatico": c.automatico, "kill_switch": c.kill_switch}
    finally:
        db.close()


@app.get("/api/mercadolivre/agentes/resumo-semana")
def ml_agentes_resumo_semana(dias: int = Query(7, ge=1, le=90), user: User = Depends(auth.get_current_user)):
    """Resumo do que a automação fez na janela: total aplicado, por agente, por dia e por gatilho."""
    from .models import AgenteExecucao
    corte = datetime.utcnow() - timedelta(days=dias)
    db = SessionLocal()
    try:
        rows = (db.query(AgenteExecucao)
                .filter(AgenteExecucao.user_id == user.id, AgenteExecucao.quando >= corte)
                .order_by(AgenteExecucao.quando.desc()).all())
    finally:
        db.close()
    tot_aplic = sum(r.aplicados or 0 for r in rows)
    tot_ign = sum(r.ignorados or 0 for r in rows)
    tot_falha = sum(r.falhas or 0 for r in rows)
    por_agente, por_dia = {}, {}
    autos = manuais = 0
    for r in rows:
        if (r.gatilho or "") == "auto":
            autos += r.aplicados or 0
        else:
            manuais += r.aplicados or 0
        dia = r.quando.strftime("%Y-%m-%d") if r.quando else "?"
        por_dia[dia] = por_dia.get(dia, 0) + (r.aplicados or 0)
        for d in (r.detalhe or []):
            if d.get("status") == "aplicado":
                ag = d.get("agente") or "outro"
                e = por_agente.setdefault(ag, {"itens": 0})
                e["itens"] += 1
    serie = [{"dia": k, "aplicados": por_dia[k]} for k in sorted(por_dia.keys())]
    return {"dias": dias, "execucoes": len(rows), "aplicados": tot_aplic, "ignorados_piso": tot_ign,
            "falhas": tot_falha, "por_gatilho": {"auto": autos, "manual": manuais},
            "por_agente": por_agente, "serie": serie}


# =========================================================================== #
# CENTRAL DE PRODUTOS (Mercado Livre) — painel: KPIs + lista filtrada.
# Lê do cache local (MLItemCache) + join Bling (ProdutoCache), aplica a regra
# de precificação (piso = menor preço cujo líquido cobre o Preço Bling).
# =========================================================================== #
def _liquido_ml_preco(cfg, preco):
    """Líquido que sobra de um preço de venda no ML, pela faixa consistente da regra."""
    if not preco or preco <= 0:
        return None
    canais = (cfg or {}).get("canais") or []
    c = next((x for x in canais if (x.get("canal") or "").lower() == "mercadolivre"), None)
    if not c:
        return None
    faixas = sorted(c.get("faixas") or [], key=lambda f: (f.get("ate") is None, f.get("ate") or 0))
    faixa = next((f for f in faixas if f.get("ate") is None or preco <= float(f.get("ate") or 0)), None)
    if not faixa and faixas:
        faixa = faixas[-1]
    if not faixa:
        return None
    imp = float(cfg.get("imposto", 0)); car = float(cfg.get("cartao", 0)); emb = float(cfg.get("embalagem", 0))
    comissao = float(faixa.get("comissao", 0)); fixo = float(faixa.get("fixo", 0)); fixo_pct = float(faixa.get("fixo_pct", 0))
    liquido = preco - preco * comissao / 100.0 - (fixo + preco * fixo_pct / 100.0) - preco * imp / 100.0 - preco * car / 100.0 - emb
    return round(liquido, 2)


_ML_STATUS_LABEL = {"active": "active", "paused": "paused", "closed": "closed",
                    "under_review": "under_review", "inactive": "inactive", "payment_required": "payment_required"}


def _produtos_painel(user_id, status=None, logistica=None, catalogo=None, saude_lt=None,
                     divergente=None, promo=None, busca=None, sort="recentes",
                     page=1, page_size=40):
    from . import mercadolivre as ml
    from .models import MLItemCache, ProdutoCache
    cfg = precificacao.obter_config(user_id)
    db = SessionLocal()
    try:
        itens = db.query(MLItemCache).filter(MLItemCache.user_id == user_id).all()
        blings = db.query(ProdutoCache).filter(ProdutoCache.user_id == user_id).all()
    finally:
        db.close()
    bling_por_sku = {}
    for b in blings:
        if b.sku:
            bling_por_sku[b.sku.strip().upper()] = b

    def _logi(lt):
        lt = (lt or "").lower()
        if lt == "fulfillment":
            return "full"
        if lt in ("self_service", "me2"):
            return "flex"
        return "normal"

    enriquecidos = []
    for it in itens:
        sku = (it.sku or "").strip()
        b = bling_por_sku.get(sku.upper()) if sku else None
        preco_bling = float(b.preco) if (b and b.preco) else None       # líquido-alvo (Preço Bling)
        saldo_bling = int(b.saldo) if (b and b.saldo is not None) else None
        piso = precificacao.preco_minimo_para_liquido(cfg, "mercadolivre", preco_bling) if preco_bling else None
        liquido = _liquido_ml_preco(cfg, it.preco) if it.preco else None
        abaixo_regra = (liquido is not None and preco_bling is not None and liquido < preco_bling - 0.01)
        estoque_ml = it.estoque if it.estoque is not None else None
        estoque_div = (saldo_bling is not None and estoque_ml is not None and abs(saldo_bling - estoque_ml) > 0)
        dados = it.dados or {}
        catalog_flag = bool(dados.get("catalog_listing")) if isinstance(dados, dict) else False
        saude_pct = round((it.saude or 0) * 100) if it.saude is not None else None
        logi = _logi(it.logistic_type)
        enriquecidos.append({
            "item_id": it.item_id, "sku": sku or None, "titulo": it.titulo,
            "preco": round(it.preco, 2) if it.preco else None,
            "preco_original": round(it.preco_original, 2) if it.preco_original else None,
            "status": it.status, "estoque": estoque_ml, "logistica": logi,
            "listing_type_id": it.listing_type_id, "catalogo": catalog_flag,
            "saude": saude_pct, "imagem": it.imagem, "permalink": it.permalink,
            "em_promocao": bool(it.em_promocao),
            "preco_bling": round(preco_bling, 2) if preco_bling else None,
            "liquido": liquido, "piso": round(piso, 2) if piso else None,
            "abaixo_regra": abaixo_regra, "tem_bling": b is not None,
            "estoque_bling": saldo_bling, "estoque_divergente": estoque_div,
            "sem_estoque": (estoque_ml == 0),
        })

    # ---- KPIs sobre o conjunto todo ----
    def _cont(p):
        return sum(1 for e in enriquecidos if p(e))
    saudes = [e["saude"] for e in enriquecidos if e["saude"] is not None]
    kpis = {
        "total": len(enriquecidos),
        "ativos": _cont(lambda e: e["status"] == "active"),
        "pausados": _cont(lambda e: e["status"] == "paused"),
        "fechados": _cont(lambda e: e["status"] == "closed"),
        "em_revisao": _cont(lambda e: e["status"] == "under_review"),
        "sem_estoque": _cont(lambda e: e["sem_estoque"]),
        "full": _cont(lambda e: e["logistica"] == "full"),
        "flex": _cont(lambda e: e["logistica"] == "flex"),
        "catalogo": _cont(lambda e: e["catalogo"]),
        "saude_media": round(sum(saudes) / len(saudes)) if saudes else None,
        "saudaveis": _cont(lambda e: (e["saude"] or 0) >= 80),
        "melhorar": _cont(lambda e: e["saude"] is not None and e["saude"] < 80),
        "abaixo_regra": _cont(lambda e: e["abaixo_regra"]),
        "sem_bling": _cont(lambda e: not e["tem_bling"]),
        "preco_divergente": _cont(lambda e: e["abaixo_regra"]),
        "estoque_divergente": _cont(lambda e: e["estoque_divergente"]),
        "em_promocao": _cont(lambda e: e["em_promocao"]),
    }

    # ---- filtros ----
    def _passa(e):
        if status and status != "todos":
            if status == "sem_estoque":
                if not e["sem_estoque"]:
                    return False
            elif e["status"] != status:
                return False
        if logistica and e["logistica"] != logistica:
            return False
        if catalogo and not e["catalogo"]:
            return False
        if promo and not e["em_promocao"]:
            return False
        if saude_lt is not None and not (e["saude"] is not None and e["saude"] < saude_lt):
            return False
        if divergente and not (e["abaixo_regra"] or e["estoque_divergente"]):
            return False
        if busca:
            q = busca.strip().lower()
            alvo = f"{e['titulo'] or ''} {e['sku'] or ''} {e['item_id'] or ''}".lower()
            if q not in alvo:
                return False
        return True

    filtrados = [e for e in enriquecidos if _passa(e)]

    # ---- ordenação ----
    if sort == "saude":
        filtrados.sort(key=lambda e: (e["saude"] is None, e["saude"] or 0))
    elif sort == "preco":
        filtrados.sort(key=lambda e: (e["preco"] or 0), reverse=True)
    elif sort == "estoque":
        filtrados.sort(key=lambda e: (e["estoque"] if e["estoque"] is not None else 1e9))
    # 'recentes' mantém a ordem do cache (já vem por atualização)

    total_filtrado = len(filtrados)
    ini = max(0, (page - 1) * page_size)
    pagina = filtrados[ini:ini + page_size]

    return {"kpis": kpis, "itens": pagina, "total_filtrado": total_filtrado,
            "page": page, "page_size": page_size,
            "cache_vazio": len(enriquecidos) == 0}


@app.get("/api/mercadolivre/produtos/painel")
def ml_produtos_painel(
    status: str = Query("todos"),
    logistica: str = Query(""),
    catalogo: bool = Query(False),
    saude_lt: int = Query(0),
    divergente: bool = Query(False),
    promo: bool = Query(False),
    busca: str = Query(""),
    sort: str = Query("recentes"),
    page: int = Query(1, ge=1),
    page_size: int = Query(40, ge=1, le=200),
    user: User = Depends(auth.get_current_user),
):
    return _produtos_painel(
        user.id, status=status or None, logistica=logistica or None,
        catalogo=catalogo or None, saude_lt=(saude_lt or None), divergente=divergente or None,
        promo=promo or None, busca=busca or None, sort=sort, page=page, page_size=page_size)


def _produto_um(user_id, item_id):
    """Item único enriquecido (mesmos campos do painel) + preço sugerido pela regra."""
    from .models import MLItemCache, ProdutoCache
    from sqlalchemy import func
    cfg = precificacao.obter_config(user_id)
    db = SessionLocal()
    try:
        it = db.query(MLItemCache).filter(MLItemCache.user_id == user_id,
                                          MLItemCache.item_id == item_id).first()
        if not it:
            return None
        b = None
        if it.sku:
            b = db.query(ProdutoCache).filter(
                ProdutoCache.user_id == user_id,
                func.upper(ProdutoCache.sku) == it.sku.strip().upper()).first()
    finally:
        db.close()
    preco_bling = float(b.preco) if (b and b.preco) else None
    saldo_bling = int(b.saldo) if (b and b.saldo is not None) else None
    piso = precificacao.preco_minimo_para_liquido(cfg, "mercadolivre", preco_bling) if preco_bling else None
    liquido = _liquido_ml_preco(cfg, it.preco) if it.preco else None
    # preço IDEAL pela regra (gross-up que preserva o líquido-alvo = Preço Bling)
    preco_regra = None
    if preco_bling:
        av = precificacao.avaliar_com_cfg(cfg, 0.0, preco_atual=preco_bling, canal="mercadolivre")
        preco_regra = av.get("preco_sugerido")
    logi = (it.logistic_type or "").lower()
    logi = "full" if logi == "fulfillment" else ("flex" if logi in ("self_service", "me2") else "normal")
    dados = it.dados or {}
    # fotos: o cache guarda só a capa; busca ao vivo todas as imagens (best-effort)
    fotos = [it.imagem] if it.imagem else []
    category_id = it.category_id
    atributos_atuais = []
    try:
        from . import mercadolivre as ml
        live = ml.obter_item(item_id, user_id=user_id)
        lf = [f for f in (live.get("fotos") or []) if f]
        if lf:
            fotos = lf
        category_id = live.get("category_id") or category_id
        atributos_atuais = [{"id": a.get("id"), "value_id": a.get("value_id"),
                             "value_name": a.get("value_name")}
                            for a in (live.get("atributos") or []) if isinstance(a, dict) and a.get("id")]
    except Exception:  # noqa: BLE001 — sem foto ao vivo não pode derrubar o cockpit
        pass
    return {
        "item_id": it.item_id, "sku": it.sku, "titulo": it.titulo,
        "preco": round(it.preco, 2) if it.preco else None,
        "status": it.status, "estoque": it.estoque, "logistica": logi,
        "listing_type_id": it.listing_type_id, "category_id": category_id,
        "atributos": atributos_atuais,
        "catalogo": bool(dados.get("catalog_listing")) if isinstance(dados, dict) else False,
        "saude": round((it.saude or 0) * 100) if it.saude is not None else None,
        "imagem": it.imagem, "fotos": fotos, "permalink": it.permalink, "em_promocao": bool(it.em_promocao),
        "preco_bling": round(preco_bling, 2) if preco_bling else None,
        "liquido": liquido, "piso": round(piso, 2) if piso else None,
        "preco_regra": round(preco_regra, 2) if preco_regra else None,
        "abaixo_regra": (liquido is not None and preco_bling is not None and liquido < preco_bling - 0.01),
        "tem_bling": b is not None, "estoque_bling": saldo_bling,
        "estoque_divergente": (saldo_bling is not None and it.estoque is not None and abs(saldo_bling - it.estoque) > 0),
        "sem_estoque": (it.estoque == 0),
    }


def _cache_item_patch(user_id, item_id, **campos):
    """Atualiza in-loco a linha do cache do anúncio (sem re-sync completo)."""
    from .models import MLItemCache
    db = SessionLocal()
    try:
        it = db.query(MLItemCache).filter(MLItemCache.user_id == user_id,
                                          MLItemCache.item_id == item_id).first()
        if it:
            for k, v in campos.items():
                if v is not None and hasattr(it, k):
                    setattr(it, k, v)
            db.commit()
    finally:
        db.close()


@app.get("/api/mercadolivre/produtos/{item_id}")
def ml_produto_um(item_id: str, user: User = Depends(auth.get_current_user)):
    r = _produto_um(user.id, item_id)
    if not r:
        raise HTTPException(status_code=404, detail="Anúncio não encontrado no cache. Sincronize o catálogo.")
    return r


@app.post("/api/mercadolivre/produtos/{item_id}/atributos")
def ml_produto_atributos(item_id: str, payload: dict = Body(...),
                         user: User = Depends(auth.get_current_user)):
    """Atualiza a ficha técnica (atributos) do anúncio no ML (PUT /items {attributes})."""
    lista = (payload or {}).get("atributos") or []
    itens = []
    for a in lista:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        entrada = {"id": a["id"]}
        if a.get("value_id"):
            entrada["value_id"] = a["value_id"]
        if a.get("value_name"):
            entrada["value_name"] = a["value_name"]
        if len(entrada) > 1:
            itens.append(entrada)
    if not itens:
        raise HTTPException(status_code=422, detail="Nenhum atributo com valor para enviar.")
    _ml_run(lambda ml: ml.atualizar_atributos(item_id, itens, user.id))
    return {"ok": True, "item_id": item_id, "enviados": len(itens)}


@app.post("/api/mercadolivre/produtos/{item_id}/editar")
def ml_produto_editar(item_id: str, payload: dict = Body(...),
                      user: User = Depends(auth.get_current_user)):
    """Edita título / preço / estoque / status de um anúncio, com TRAVA DE PISO no preço.
    Body: {titulo?, preco?, estoque?, status?, permitir_abaixo_piso?}. Aplica só o que veio."""
    from . import mercadolivre as ml
    titulo = payload.get("titulo")
    preco = payload.get("preco")
    estoque = payload.get("estoque")
    status = payload.get("status")
    permitir = bool(payload.get("permitir_abaixo_piso"))

    aplicados = {}
    patch = {}

    # ---- preço: golden rule (nunca abaixo do piso, salvo override manual) ----
    if preco is not None:
        try:
            preco = round(float(preco), 2)
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Preço inválido.")
        if preco <= 0:
            raise HTTPException(status_code=422, detail="Preço deve ser maior que zero.")
        _, piso = _piso_item(user.id, item_id)
        if piso is not None and preco < piso - 0.01 and not permitir:
            raise HTTPException(status_code=400, detail={
                "erro": "abaixo_do_piso",
                "mensagem": f"R$ {preco:.2f} fica abaixo do piso de R$ {piso:.2f} (fura a margem).",
                "piso": round(piso, 2), "minimo_seguro": round(piso, 2),
            })

    # ---- aplica cada mudança que veio ----
    if titulo is not None and str(titulo).strip():
        _ml_run(lambda ml: ml.atualizar_titulo(item_id, str(titulo), user.id))
        aplicados["titulo"] = str(titulo).strip()[:60]
        patch["titulo"] = aplicados["titulo"]
    if status is not None:
        if status not in ("active", "paused", "closed"):
            raise HTTPException(status_code=422, detail="Status deve ser active | paused | closed.")
        _ml_run(lambda ml: ml.atualizar_status(item_id, status, user.id))
        aplicados["status"] = status
        patch["status"] = status
    if estoque is not None:
        _ml_run(lambda ml: ml.atualizar_estoque(item_id, int(estoque), user.id))
        aplicados["estoque"] = int(estoque)
        patch["estoque"] = int(estoque)
    if preco is not None:
        _ml_run(lambda ml: ml.atualizar_preco(item_id, preco, user.id))
        aplicados["preco"] = preco
        patch["preco"] = preco

    if not aplicados:
        raise HTTPException(status_code=422, detail="Nada para editar. Envie ao menos um campo.")

    _cache_item_patch(user.id, item_id, **patch)
    return {"ok": True, "item_id": item_id, "aplicados": aplicados,
            "produto": _produto_um(user.id, item_id)}


# --------------------------------------------------------------------------- #
# IA do anúncio — Copiloto (título e descrição). Usa o Gemini (ai._gerar_texto).
# --------------------------------------------------------------------------- #
def _linhas_ia(txt: str, n: int = 3) -> list:
    """Extrai até N linhas limpas de um retorno da IA (tira número, bullet, aspas, cerca)."""
    import re
    out = []
    for ln in (txt or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        s = re.sub(r"^```[a-z]*", "", s).strip()
        if s.startswith("```"):
            continue
        s = re.sub(r"^\s*(\d+[\.\)\-]|[\-\*•])\s*", "", s)   # 1. / 2) / - / *
        s = s.strip().strip('"').strip("'").strip("`").strip()
        if s and len(s) > 3:
            out.append(s[:60])
        if len(out) >= n:
            break
    return out


def _titulo_atual(user_id, item_id, titulo):
    if titulo:
        return titulo
    from .models import MLItemCache
    db = SessionLocal()
    try:
        it = db.query(MLItemCache).filter(MLItemCache.user_id == user_id,
                                          MLItemCache.item_id == item_id).first()
        return (it.titulo if it else "") or ""
    finally:
        db.close()


@app.post("/api/mercadolivre/produtos/ia/titulo")
def ml_produto_ia_titulo(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Sugere 3 títulos otimizados para o Mercado Livre (≤60 car., sem palavras proibidas)."""
    item_id = (payload or {}).get("item_id")
    atual = _titulo_atual(user.id, item_id, (payload or {}).get("titulo"))
    if not atual.strip():
        raise HTTPException(status_code=422, detail="Sem título de base. Informe item_id ou titulo.")
    categoria = (payload or {}).get("categoria") or ""
    prompt = (
        "Você é especialista em SEO de marketplace no Mercado Livre Brasil.\n"
        "Reescreva o título do anúncio abaixo em 3 variações otimizadas para busca.\n"
        "Regras OBRIGATÓRIAS:\n"
        "- No máximo 60 caracteres cada.\n"
        "- Estrutura: Produto + Marca/Modelo + atributo-chave (tamanho, cor, material, quantidade).\n"
        "- NÃO use: 'frete grátis', 'promoção', 'oferta', 'imperdível', emojis, LETRAS TODAS MAIÚSCULAS.\n"
        "- Sem repetir palavras. Português do Brasil.\n"
        "Responda APENAS as 3 opções, uma por linha, sem numeração e sem comentários.\n\n"
        f"Categoria: {categoria}\n"
        f"Título atual: {atual}\n"
    )
    txt = ai._gerar_texto(user.id, prompt)
    sugestoes = _linhas_ia(txt, 3)
    if not sugestoes:
        raise HTTPException(status_code=502, detail="A IA não retornou sugestões. Tente novamente.")
    return {"atual": atual, "sugestoes": sugestoes, "melhor": sugestoes[0]}


@app.post("/api/mercadolivre/produtos/ia/descricao")
def ml_produto_ia_descricao(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Gera uma descrição em texto puro (o ML não aceita HTML desde 2021)."""
    item_id = (payload or {}).get("item_id")
    nome = _titulo_atual(user.id, item_id, (payload or {}).get("titulo"))
    if not nome.strip():
        raise HTTPException(status_code=422, detail="Sem título de base. Informe item_id ou titulo.")
    caracteristicas = (payload or {}).get("caracteristicas") or ""
    prompt = (
        "Você é redator de anúncios do Mercado Livre Brasil. Escreva uma descrição vendedora "
        "em TEXTO PURO (sem HTML, sem markdown), pronta para colar no anúncio.\n"
        "Estrutura: 1 parágrafo de abertura com o benefício principal; depois uma lista curta de "
        "características (uma por linha, com '- '); feche com uma linha de reforço.\n"
        "Seja específico com medidas e materiais. Não invente marca nem garantia. Português do Brasil.\n\n"
        f"Produto: {nome}\n"
        f"Características informadas: {caracteristicas or '(use o que dá para inferir do título)'}\n"
    )
    texto = ai._gerar_texto(user.id, prompt)
    texto = (texto or "").strip()
    if not texto:
        raise HTTPException(status_code=502, detail="A IA não retornou descrição. Tente novamente.")
    return {"texto": texto, "chars": len(texto)}


# =========================================================================== #
# MOTOR DE PUBLICAÇÃO — origem Bling, categoria, atributos, validação, criação.
# =========================================================================== #
@app.get("/api/mercadolivre/publicar/bling")
def ml_publicar_bling(busca: str = Query(""), somente_novos: bool = Query(False),
                      page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100),
                      user: User = Depends(auth.get_current_user)):
    """Produtos do Bling candidatos a publicar, com flag se já existem no ML e preço sugerido."""
    from .models import ProdutoCache, MLItemCache
    cfg = precificacao.obter_config(user.id)
    db = SessionLocal()
    try:
        skus_ml = {(x.sku or "").strip().upper() for x in
                   db.query(MLItemCache.sku).filter(MLItemCache.user_id == user.id).all() if x.sku}
        q = db.query(ProdutoCache).filter(ProdutoCache.user_id == user.id)
        if busca.strip():
            like = f"%{busca.strip()}%"
            q = q.filter((ProdutoCache.nome.ilike(like)) | (ProdutoCache.sku.ilike(like)))
        produtos = q.all()
    finally:
        db.close()
    itens = []
    for p in produtos:
        sku_up = (p.sku or "").strip().upper()
        ja = bool(sku_up and sku_up in skus_ml)
        if somente_novos and ja:
            continue
        preco_bling = float(p.preco) if p.preco else None
        preco_regra = None
        if preco_bling:
            av = precificacao.avaliar_com_cfg(cfg, 0.0, preco_atual=preco_bling, canal="mercadolivre")
            preco_regra = av.get("preco_sugerido")
        _d = p.dados or {}
        _ext = (((_d.get("midia") or {}).get("imagens") or {}).get("externas") or []) if isinstance(_d, dict) else []
        imagens = [i.get("link") for i in _ext if isinstance(i, dict) and i.get("link")]
        if not imagens and p.imagem:
            imagens = [p.imagem]
        itens.append({
            "produto_id": p.produto_id, "sku": p.sku, "nome": p.nome, "imagem": p.imagem,
            "imagens": imagens,
            "preco_bling": round(preco_bling, 2) if preco_bling else None,
            "preco_regra": round(preco_regra, 2) if preco_regra else None,
            "saldo": int(p.saldo) if p.saldo is not None else 0,
            "ja_no_ml": ja,
        })
    total = len(itens)
    novos = sum(1 for i in itens if not i["ja_no_ml"])
    ini = (page - 1) * page_size
    return {"itens": itens[ini:ini + page_size], "total": total, "novos": novos,
            "page": page, "page_size": page_size}


@app.get("/api/mercadolivre/categorias/prever")
def ml_categoria_prever(titulo: str = Query(...), user: User = Depends(auth.get_current_user)):
    if not titulo.strip():
        raise HTTPException(status_code=422, detail="Informe o título para prever a categoria.")
    return {"sugestoes": _ml_run(lambda ml: ml.prever_categoria(titulo, user.id))}


_ATTR_IMPORTANTES = {
    "BRAND", "MODEL", "GTIN", "EAN", "UPC", "MATERIAL", "COLOR", "MAIN_COLOR",
    "SECONDARY_COLOR", "SIZE", "LENGTH", "WIDTH", "HEIGHT", "DEPTH", "DIAMETER",
    "WEIGHT", "NET_WEIGHT", "LINE", "MPN", "UNITS_PER_PACKAGE", "ITEMS_PER_PACK",
    "AGE_GROUP", "GENDER", "FORMAT", "CAPACITY", "VOLUME", "QUANTITY",
}


def _atributos_uteis(bruto):
    """Cura os atributos da categoria: descarta o que é do sistema (hidden/read_only/fixed)
    e marca os relevantes (obrigatórios + campos que o vendedor de fato usa)."""
    obrig, rec = [], []
    for a in (bruto or []):
        if not isinstance(a, dict) or not a.get("id"):
            continue
        aid = a.get("id")
        tags = a.get("tags") or {}
        # fora: atributos de sistema, não editáveis ou ocultos no painel do vendedor
        if tags.get("hidden") or tags.get("read_only") or tags.get("fixed"):
            continue
        vtype = a.get("value_type") or "string"
        valores = [{"id": v.get("id"), "nome": v.get("name")}
                   for v in (a.get("values") or []) if isinstance(v, dict)][:60]
        obrigatorio = bool(tags.get("required") or tags.get("catalog_required")
                           or tags.get("catalog_listing_required"))
        unidades = [u.get("name") for u in (a.get("allowed_units") or []) if isinstance(u, dict)]
        default_unit = a.get("default_unit")
        if not default_unit and unidades:
            default_unit = unidades[0]
        relevancia = a.get("relevance")
        if relevancia is None:
            relevancia = 1 if aid in _ATTR_IMPORTANTES else 90
        relevante = obrigatorio or aid in _ATTR_IMPORTANTES or (isinstance(relevancia, int) and relevancia <= 2)
        item = {
            "id": aid, "nome": a.get("name"), "obrigatorio": obrigatorio,
            "tipo": vtype, "valores": valores,
            "permite_livre": vtype in ("string", "number", "number_unit") or not valores,
            "unidades": unidades, "unidade": default_unit,
            "variacao": bool(tags.get("allow_variations") or tags.get("variation_attribute")),
            "gtin": aid in ("GTIN", "EAN", "UPC"),
            "relevancia": relevancia, "relevante": bool(relevante),
        }
        (obrig if obrigatorio else rec).append(item)
    # obrigatórios primeiro; recomendados por relevância (útil antes do cauda-longa)
    obrig.sort(key=lambda x: (x["relevancia"], x["nome"] or ""))
    rec.sort(key=lambda x: (not x["relevante"], x["relevancia"], x["nome"] or ""))
    return obrig + rec


@app.get("/api/mercadolivre/categorias/{category_id}/atributos")
def ml_categoria_atributos(category_id: str, user: User = Depends(auth.get_current_user)):
    bruto = _ml_run(lambda ml: ml.atributos_categoria(category_id, user.id))
    uteis = _atributos_uteis(bruto)
    return {"category_id": category_id, "atributos": uteis,
            "obrigatorios": sum(1 for a in uteis if a["obrigatorio"]),
            "relevantes": sum(1 for a in uteis if a["relevante"] and not a["obrigatorio"]),
            "total": len(uteis)}


@app.post("/api/mercadolivre/categorias/{category_id}/atributos/ia")
def ml_categoria_atributos_ia(category_id: str, payload: dict = Body(...),
                              user: User = Depends(auth.get_current_user)):
    """IA preenche valores de atributos a partir do título. Retorna sugestões (revisáveis)."""
    titulo = (payload or {}).get("titulo") or ""
    if not titulo.strip():
        raise HTTPException(status_code=422, detail="Informe o título para a IA inferir atributos.")
    bruto = _ml_run(lambda ml: ml.atributos_categoria(category_id, user.id))
    uteis = _atributos_uteis(bruto)
    # foca nos mais úteis: obrigatórios + alguns comuns
    comuns = {"BRAND", "MODEL", "COLOR", "MATERIAL", "GTIN", "LINE", "SIZE",
              "PACKAGE_LENGTH", "ITEM_CONDITION"}
    alvo = [a for a in uteis if a["obrigatorio"] or a["id"] in comuns][:14]
    if not alvo:
        return {"sugestoes": []}
    linhas = "\n".join(f"- {a['id']} ({a['nome']})" for a in alvo)
    prompt = (
        "Você preenche fichas técnicas de produtos do Mercado Livre.\n"
        "A partir do título abaixo, infira o valor de cada atributo listado. "
        "Se não der para inferir com segurança, deixe vazio.\n"
        "Responda em linhas no formato ATRIBUTO_ID=valor (uma por linha), sem comentários.\n\n"
        f"Título: {titulo}\n\nAtributos:\n{linhas}\n"
    )
    txt = ai._gerar_texto(user.id, prompt)
    mapa = {}
    for ln in (txt or "").splitlines():
        if "=" in ln:
            kk, _, vv = ln.partition("=")
            kk = kk.strip().strip("-").strip().upper()
            vv = vv.strip().strip('"').strip("'").strip()
            if kk and vv and vv.lower() not in ("vazio", "n/a", "na", "-", "nulo", "none"):
                mapa[kk] = vv
    por_id = {a["id"]: a for a in alvo}
    sugestoes = []
    for aid, val in mapa.items():
        a = por_id.get(aid)
        if not a:
            continue
        vid = None
        for v in a["valores"]:
            if (v["nome"] or "").strip().lower() == val.strip().lower():
                vid = v["id"]
                break
        sugestoes.append({"id": aid, "nome": a["nome"], "value_name": val,
                          "value_id": vid, "obrigatorio": a["obrigatorio"]})
    return {"sugestoes": sugestoes}


def _montar_item_body(titulo, category_id, preco, quantidade, listing_type_id="gold_special",
                      condicao="new", pictures=None, atributos=None, sku=None, descricao=None,
                      variations=None):
    body = {
        "title": (titulo or "").strip()[:60],
        "category_id": category_id,
        "price": round(float(preco), 2),
        "currency_id": "BRL",
        "available_quantity": int(quantidade),
        "buying_mode": "buy_it_now",
        "listing_type_id": listing_type_id or "gold_special",
        "condition": condicao or "new",
        "pictures": [{"source": u} for u in (pictures or []) if u],
    }
    attrs = []
    for a in (atributos or []):
        if not isinstance(a, dict) or not a.get("id"):
            continue
        item = {"id": a["id"]}
        if a.get("value_id"):
            item["value_id"] = a["value_id"]
        if a.get("value_name"):
            item["value_name"] = a["value_name"]
        attrs.append(item)
    if sku and not any(a.get("id") == "SELLER_SKU" for a in attrs):
        attrs.append({"id": "SELLER_SKU", "value_name": str(sku)})
    if attrs:
        body["attributes"] = attrs
    # variações: cada uma com sua combinação de atributo + estoque (preço opcional por variação)
    vlist = []
    for v in (variations or []):
        comb = v.get("attribute_combinations") or []
        aq = v.get("available_quantity")
        comb_ok = [c for c in comb if isinstance(c, dict) and c.get("id") and (c.get("value_id") or c.get("value_name"))]
        if not comb_ok or aq is None:
            continue
        entrada = {"attribute_combinations": comb_ok, "available_quantity": int(aq)}
        if v.get("price"):
            entrada["price"] = round(float(v["price"]), 2)
        if v.get("seller_custom_field"):
            entrada["seller_custom_field"] = str(v["seller_custom_field"])
        if v.get("picture_ids"):
            entrada["picture_ids"] = v["picture_ids"]
        vlist.append(entrada)
    if vlist:
        body["variations"] = vlist
        body.pop("available_quantity", None)  # o estoque passa a ser por variação
    return body


def _piso_guard_preco(user_id, sku, preco, permitir):
    """Aplica a golden rule usando o Preço Bling do SKU. Levanta HTTPException se furar."""
    if not sku:
        return
    from .models import ProdutoCache
    from sqlalchemy import func
    db = SessionLocal()
    try:
        b = db.query(ProdutoCache).filter(ProdutoCache.user_id == user_id,
                                          func.upper(ProdutoCache.sku) == sku.strip().upper()).first()
    finally:
        db.close()
    if not b or not b.preco:
        return
    cfg = precificacao.obter_config(user_id)
    piso = precificacao.preco_minimo_para_liquido(cfg, "mercadolivre", float(b.preco))
    if piso is not None and preco < piso - 0.01 and not permitir:
        raise HTTPException(status_code=400, detail={
            "erro": "abaixo_do_piso",
            "mensagem": f"R$ {preco:.2f} fica abaixo do piso de R$ {piso:.2f} (fura a margem).",
            "piso": round(piso, 2), "minimo_seguro": round(piso, 2)})


@app.post("/api/mercadolivre/produtos/validar")
def ml_produto_validar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Monta o corpo do anúncio e roda POST /items/validate — devolve erros/avisos do ML."""
    titulo = (payload or {}).get("titulo")
    category_id = (payload or {}).get("category_id")
    preco = (payload or {}).get("preco")
    quantidade = (payload or {}).get("quantidade")
    variations = (payload or {}).get("variations") or []
    if variations:
        soma = sum(int(v.get("available_quantity") or 0) for v in variations if isinstance(v, dict))
        if soma > 0:
            quantidade = soma
    if not titulo or not category_id or preco is None or quantidade is None:
        raise HTTPException(status_code=422, detail="Informe título, categoria, preço e quantidade.")
    body = _montar_item_body(
        titulo, category_id, preco, quantidade,
        listing_type_id=payload.get("listing_type_id"), condicao=payload.get("condicao"),
        pictures=payload.get("pictures"), atributos=payload.get("atributos"),
        sku=payload.get("sku"), descricao=payload.get("descricao"), variations=variations)
    res = _ml_run(lambda ml: ml.validar_item(body, user.id))
    return {**res, "body": body}


@app.post("/api/mercadolivre/produtos/publicar")
def ml_produto_publicar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Publica o anúncio (POST /items) com trava de piso; grava no cache; devolve o item."""
    from . import mercadolivre as ml
    from .models import MLItemCache
    titulo = (payload or {}).get("titulo")
    category_id = (payload or {}).get("category_id")
    preco = (payload or {}).get("preco")
    quantidade = (payload or {}).get("quantidade")
    sku = (payload or {}).get("sku")
    variations = (payload or {}).get("variations") or []
    # com variações, o estoque é a soma das variações
    if variations:
        soma = sum(int(v.get("available_quantity") or 0) for v in variations if isinstance(v, dict))
        if soma > 0:
            quantidade = soma
    if not titulo or not category_id or preco is None or quantidade is None:
        raise HTTPException(status_code=422, detail="Informe título, categoria, preço e quantidade.")
    try:
        preco = round(float(preco), 2)
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Preço inválido.")
    _piso_guard_preco(user.id, sku, preco, bool(payload.get("permitir_abaixo_piso")))
    body = _montar_item_body(
        titulo, category_id, preco, quantidade,
        listing_type_id=payload.get("listing_type_id"), condicao=payload.get("condicao"),
        pictures=payload.get("pictures"), atributos=payload.get("atributos"),
        sku=sku, descricao=payload.get("descricao"), variations=variations)
    criado = _ml_run(lambda ml: ml.publicar_item(body, user.id))
    item_id = criado.get("id") if isinstance(criado, dict) else None
    # descrição (endpoint separado no ML)
    descricao = (payload or {}).get("descricao")
    if item_id and descricao and str(descricao).strip():
        try:
            _ml_run(lambda ml: ml.atualizar_descricao(item_id, str(descricao).strip(), user.id))
        except Exception:
            pass
    # grava no cache local
    if item_id:
        db = SessionLocal()
        try:
            if not db.query(MLItemCache).filter(MLItemCache.user_id == user.id,
                                                MLItemCache.item_id == item_id).first():
                db.add(MLItemCache(
                    user_id=user.id, item_id=item_id, sku=sku,
                    titulo=criado.get("title"), preco=criado.get("price"),
                    status=criado.get("status"), estoque=criado.get("available_quantity"),
                    category_id=criado.get("category_id"),
                    listing_type_id=criado.get("listing_type_id"),
                    logistic_type=((criado.get("shipping") or {}).get("logistic_type")),
                    permalink=criado.get("permalink"),
                    imagem=((criado.get("pictures") or [{}])[0].get("url") if criado.get("pictures") else None),
                    dados={"catalog_listing": criado.get("catalog_listing", False)}))
                db.commit()
        finally:
            db.close()
    return {"ok": True, "item_id": item_id, "permalink": criado.get("permalink") if isinstance(criado, dict) else None,
            "status": criado.get("status") if isinstance(criado, dict) else None, "criado": criado}


# =========================================================================== #
# SINCRONIZAÇÃO Bling ↔ ML — divergências linha-a-linha (preço/estoque/órfãos).
# As ações (reprecificar/igualar estoque) reusam o endpoint de edição de item.
# =========================================================================== #
def _divergencias(user_id, tipo="preco", page=1, page_size=40):
    from .models import MLItemCache, ProdutoCache
    cfg = precificacao.obter_config(user_id)
    db = SessionLocal()
    try:
        itens = db.query(MLItemCache).filter(MLItemCache.user_id == user_id).all()
        blings = db.query(ProdutoCache).filter(ProdutoCache.user_id == user_id).all()
    finally:
        db.close()
    bling_por_sku = {}
    for b in blings:
        if b.sku:
            bling_por_sku[b.sku.strip().upper()] = b

    def _regra(preco_bling):
        if not preco_bling:
            return None
        av = precificacao.avaliar_com_cfg(cfg, 0.0, preco_atual=float(preco_bling), canal="mercadolivre")
        return av.get("preco_sugerido")

    skus_ml = set()
    preco_list, estoque_list = [], []
    for it in itens:
        sku = (it.sku or "").strip()
        if sku:
            skus_ml.add(sku.upper())
        b = bling_por_sku.get(sku.upper()) if sku else None
        preco_bling = float(b.preco) if (b and b.preco) else None
        saldo_bling = int(b.saldo) if (b and b.saldo is not None) else None
        liquido = _liquido_ml_preco(cfg, it.preco) if it.preco else None
        piso = precificacao.preco_minimo_para_liquido(cfg, "mercadolivre", preco_bling) if preco_bling else None
        if liquido is not None and preco_bling is not None and liquido < preco_bling - 0.01:
            preco_regra = _regra(preco_bling)
            preco_list.append({
                "item_id": it.item_id, "sku": sku or None, "titulo": it.titulo, "imagem": it.imagem,
                "preco": round(it.preco, 2) if it.preco else None,
                "liquido": liquido, "preco_bling": round(preco_bling, 2),
                "piso": round(piso, 2) if piso else None,
                "preco_regra": round(preco_regra, 2) if preco_regra else None,
                "delta": round(preco_bling - liquido, 2),
            })
        if saldo_bling is not None and it.estoque is not None and saldo_bling != it.estoque:
            estoque_list.append({
                "item_id": it.item_id, "sku": sku or None, "titulo": it.titulo, "imagem": it.imagem,
                "estoque_ml": it.estoque, "estoque_bling": saldo_bling,
                "diff": saldo_bling - it.estoque,
            })

    orfaos = []
    for b in blings:
        sku_up = (b.sku or "").strip().upper()
        if not sku_up or sku_up not in skus_ml:
            preco_bling = float(b.preco) if b.preco else None
            orfaos.append({
                "produto_id": b.produto_id, "sku": b.sku, "nome": b.nome, "imagem": b.imagem,
                "saldo": int(b.saldo) if b.saldo is not None else 0,
                "preco_bling": round(preco_bling, 2) if preco_bling else None,
                "preco_regra": round(_regra(preco_bling), 2) if preco_bling else None,
            })

    counts = {"preco": len(preco_list), "estoque": len(estoque_list), "orfaos": len(orfaos),
              "total_ml": len(itens), "total_bling": len(blings)}
    em_dia = max(0, len(itens) - len(preco_list) - len(estoque_list))
    counts["em_dia"] = em_dia
    sel = {"preco": preco_list, "estoque": estoque_list, "orfaos": orfaos}.get(tipo, preco_list)
    total = len(sel)
    ini = max(0, (page - 1) * page_size)
    return {"counts": counts, "itens": sel[ini:ini + page_size], "total": total,
            "tipo": tipo, "page": page, "page_size": page_size}


@app.get("/api/mercadolivre/sync/divergencias")
def ml_sync_divergencias(tipo: str = Query("preco"), page: int = Query(1, ge=1),
                         page_size: int = Query(40, ge=1, le=200),
                         user: User = Depends(auth.get_current_user)):
    if tipo not in ("preco", "estoque", "orfaos"):
        tipo = "preco"
    return _divergencias(user.id, tipo=tipo, page=page, page_size=page_size)


# =========================================================================== #
# FISCAL (bloqueante) — prontidão + cadastro NCM/origem, Bling como fonte.
# can_invoice é 1 req/item: o painel usa o NCM do Bling como proxy de prontidão;
# o drill-down por item faz a checagem real no ML.
# =========================================================================== #
_ORIGENS_NACIONAIS = {"0", "3", "4", "5", "8"}   # SEFAZ: nacionais; 1/2/6/7 = importado


def _ncm_bling(dados):
    if not isinstance(dados, dict):
        return ""
    ncm = (dados.get("tributacao") or {}).get("ncm") or dados.get("ncm") or ""
    return "".join(ch for ch in str(ncm) if ch.isdigit())


def _fiscal_sugerido_bling(dados):
    """Deriva NCM + origem do produto do Bling para pré-preencher o ML."""
    ncm = _ncm_bling(dados)
    origem = None
    cest = ""
    if isinstance(dados, dict):
        trib = dados.get("tributacao") or {}
        origem = trib.get("origem")
        cest = "".join(ch for ch in str(trib.get("cest") or "") if ch.isdigit())
    origin_detail = str(origem) if origem not in (None, "") else "0"
    nacional = origin_detail in _ORIGENS_NACIONAIS
    return {
        "ncm": ncm if len(ncm) == 8 else "",
        "origin_type": "reseller" if nacional else "imported",
        "origin_detail": origin_detail,
        "cest": cest,
        "tem_ncm": len(ncm) == 8,
    }


def _fiscal_painel(user_id, situacao="todos", busca=None, page=1, page_size=40):
    from .models import MLItemCache, ProdutoCache
    from sqlalchemy import func
    db = SessionLocal()
    try:
        itens = db.query(MLItemCache).filter(MLItemCache.user_id == user_id).all()
        blings = db.query(ProdutoCache).filter(ProdutoCache.user_id == user_id).all()
    finally:
        db.close()
    bling_por_sku = {}
    for b in blings:
        if b.sku:
            bling_por_sku[b.sku.strip().upper()] = b
    linhas = []
    for it in itens:
        sku = (it.sku or "").strip()
        b = bling_por_sku.get(sku.upper()) if sku else None
        sug = _fiscal_sugerido_bling(b.dados if b else None)
        if b is None:
            estado = "sem_bling"
        elif sug["tem_ncm"]:
            estado = "pronto"          # tem NCM no Bling → dá para enviar
        else:
            estado = "sem_ncm"
        linhas.append({
            "item_id": it.item_id, "sku": sku or None, "titulo": it.titulo, "imagem": it.imagem,
            "tem_bling": b is not None, "ncm": sug["ncm"], "origin_type": sug["origin_type"],
            "origin_detail": sug["origin_detail"], "cest": sug["cest"], "estado": estado,
        })
    kpis = {
        "total": len(linhas),
        "pronto": sum(1 for x in linhas if x["estado"] == "pronto"),
        "sem_ncm": sum(1 for x in linhas if x["estado"] == "sem_ncm"),
        "sem_bling": sum(1 for x in linhas if x["estado"] == "sem_bling"),
    }

    def _passa(x):
        if situacao and situacao != "todos" and x["estado"] != situacao:
            return False
        if busca:
            q = busca.strip().lower()
            if q not in f"{x['titulo'] or ''} {x['sku'] or ''} {x['item_id']}".lower():
                return False
        return True

    filtrados = [x for x in linhas if _passa(x)]
    total = len(filtrados)
    ini = max(0, (page - 1) * page_size)
    return {"kpis": kpis, "itens": filtrados[ini:ini + page_size], "total": total,
            "situacao": situacao, "page": page, "page_size": page_size}


@app.get("/api/mercadolivre/fiscal/painel")
def ml_fiscal_painel(situacao: str = Query("todos"), busca: str = Query(""),
                     page: int = Query(1, ge=1), page_size: int = Query(40, ge=1, le=200),
                     user: User = Depends(auth.get_current_user)):
    if situacao not in ("todos", "pronto", "sem_ncm", "sem_bling"):
        situacao = "todos"
    return _fiscal_painel(user.id, situacao=situacao, busca=busca or None, page=page, page_size=page_size)


@app.get("/api/mercadolivre/fiscal/regras")
def ml_fiscal_regras(user: User = Depends(auth.get_current_user)):
    regras = _ml_run(lambda ml: ml.regras_fiscais(user.id))
    out = [{"id": r.get("id"), "description": r.get("description")}
           for r in (regras or []) if isinstance(r, dict)]
    return {"regras": out}


@app.get("/api/mercadolivre/fiscal/item/{item_id}")
def ml_fiscal_item(item_id: str, user: User = Depends(auth.get_current_user)):
    """Prontidão real (can_invoice) + fiscal atual no ML + sugestão vinda do Bling."""
    from .models import MLItemCache, ProdutoCache
    from sqlalchemy import func
    db = SessionLocal()
    try:
        it = db.query(MLItemCache).filter(MLItemCache.user_id == user.id,
                                          MLItemCache.item_id == item_id).first()
        b = None
        if it and it.sku:
            b = db.query(ProdutoCache).filter(
                ProdutoCache.user_id == user.id,
                func.upper(ProdutoCache.sku) == it.sku.strip().upper()).first()
    finally:
        db.close()
    if not it:
        raise HTTPException(status_code=404, detail="Anúncio não encontrado no cache.")
    sku = (it.sku or "").strip()
    sug = _fiscal_sugerido_bling(b.dados if b else None)
    can = _ml_run(lambda ml: ml.pode_faturar(item_id, user.id))
    atual = _ml_run(lambda ml: ml.fiscal_do_item(sku, user.id)) if sku else None
    return {
        "item_id": item_id, "sku": sku or None, "titulo": it.titulo,
        "pode_faturar": (can or {}).get("status"),
        "fiscal_atual": (atual.get("tax_information") if isinstance(atual, dict) else None),
        "sugestao_bling": sug, "tem_bling": b is not None,
    }


@app.post("/api/mercadolivre/fiscal/item/{item_id}")
def ml_fiscal_salvar(item_id: str, payload: dict = Body(None),
                     user: User = Depends(auth.get_current_user)):
    """Grava dados fiscais do item (POST /items/fiscal_information) e rec]heca can_invoice.
    Sem payload, usa o que veio do Bling (NCM/origem)."""
    from .models import MLItemCache, ProdutoCache
    from sqlalchemy import func
    payload = payload or {}
    db = SessionLocal()
    try:
        it = db.query(MLItemCache).filter(MLItemCache.user_id == user.id,
                                          MLItemCache.item_id == item_id).first()
        b = None
        if it and it.sku:
            b = db.query(ProdutoCache).filter(
                ProdutoCache.user_id == user.id,
                func.upper(ProdutoCache.sku) == it.sku.strip().upper()).first()
    finally:
        db.close()
    if not it or not (it.sku or "").strip():
        raise HTTPException(status_code=422, detail="Item sem SKU — o cadastro fiscal do ML é por SKU.")
    sku = it.sku.strip()
    sug = _fiscal_sugerido_bling(b.dados if b else None)
    ncm = "".join(ch for ch in str(payload.get("ncm") or sug["ncm"] or "") if ch.isdigit())
    if len(ncm) != 8:
        raise HTTPException(status_code=422, detail={
            "erro": "ncm_invalido",
            "mensagem": "NCM de 8 dígitos é obrigatório. Cadastre o NCM no Bling ou informe manualmente."})
    tax = {"ncm": ncm,
           "origin_type": payload.get("origin_type") or sug["origin_type"],
           "origin_detail": str(payload.get("origin_detail") or sug["origin_detail"] or "0")}
    if payload.get("cest") or sug["cest"]:
        tax["cest"] = "".join(ch for ch in str(payload.get("cest") or sug["cest"]) if ch.isdigit())
    if payload.get("csosn"):
        tax["csosn"] = str(payload["csosn"])
    if payload.get("ean"):
        tax["ean"] = str(payload["ean"])
    if payload.get("tax_rule_id"):            # só Regime Normal
        try:
            tax["tax_rule_id"] = int(payload["tax_rule_id"])
        except (TypeError, ValueError):
            pass
    _ml_run(lambda ml: ml.salvar_fiscal(sku, tax, user.id))
    can = _ml_run(lambda ml: ml.pode_faturar(item_id, user.id))
    return {"ok": True, "item_id": item_id, "sku": sku, "enviado": tax,
            "pode_faturar": (can or {}).get("status")}


# =========================================================================== #
# WEBHOOKS — painel em tempo real (eventos por tópico, latência, recuperação).
# =========================================================================== #
_ML_TOPICOS = ["items", "items_prices", "stock_locations", "catalog_item_competition",
               "price_suggestion", "moderations_reports", "shipments", "orders_v2", "messages"]


@app.get("/api/mercadolivre/webhooks/painel")
def ml_webhooks_painel(horas: int = Query(24, ge=1, le=168),
                       user: User = Depends(auth.get_current_user)):
    from .models import MLWebhookEvento
    from sqlalchemy import func
    limite = datetime.utcnow() - timedelta(hours=horas)
    db = SessionLocal()
    try:
        base = db.query(MLWebhookEvento).filter(MLWebhookEvento.user_id == user.id)
        total_geral = base.count()
        janela = base.filter(MLWebhookEvento.recebido_em >= limite)
        total_janela = janela.count()
        processados = janela.filter(MLWebhookEvento.processado == True).count()  # noqa: E712
        # por tópico (na janela)
        por_topico_raw = dict(
            db.query(MLWebhookEvento.topic, func.count(MLWebhookEvento.id))
            .filter(MLWebhookEvento.user_id == user.id, MLWebhookEvento.recebido_em >= limite)
            .group_by(MLWebhookEvento.topic).all())
        # último recebido por tópico (geral)
        ultimo_raw = dict(
            db.query(MLWebhookEvento.topic, func.max(MLWebhookEvento.recebido_em))
            .filter(MLWebhookEvento.user_id == user.id)
            .group_by(MLWebhookEvento.topic).all())
        recentes = (db.query(MLWebhookEvento)
                    .filter(MLWebhookEvento.user_id == user.id)
                    .order_by(MLWebhookEvento.recebido_em.desc()).limit(30).all())
        ultimo_geral = db.query(func.max(MLWebhookEvento.recebido_em)).filter(
            MLWebhookEvento.user_id == user.id).scalar()
    finally:
        db.close()

    por_topico = [{"topic": t, "n": por_topico_raw.get(t, 0),
                   "ultimo": ultimo_raw.get(t).isoformat() if ultimo_raw.get(t) else None}
                  for t in _ML_TOPICOS if por_topico_raw.get(t)]
    # tópicos conhecidos sem eventos entram com 0 (para o gráfico ficar completo)
    for t in _ML_TOPICOS:
        if t not in por_topico_raw:
            por_topico.append({"topic": t, "n": 0,
                               "ultimo": ultimo_raw.get(t).isoformat() if ultimo_raw.get(t) else None})
    por_topico.sort(key=lambda x: x["n"], reverse=True)

    lista = [{"topic": e.topic, "resource_id": e.resource_id, "processado": bool(e.processado),
              "resultado": e.resultado, "attempts": e.attempts,
              "recebido_em": e.recebido_em.isoformat() if e.recebido_em else None} for e in recentes]

    taxa = round((processados / total_janela) * 100) if total_janela else None
    return {
        "conectado": total_geral > 0,
        "total_geral": total_geral, "janela_horas": horas, "total_janela": total_janela,
        "processados": processados, "taxa_processamento": taxa,
        "ultimo_recebido": ultimo_geral.isoformat() if ultimo_geral else None,
        "por_topico": por_topico, "recentes": lista, "topicos_assinados": _ML_TOPICOS,
    }


@app.post("/api/mercadolivre/webhooks/recuperar")
def ml_webhooks_recuperar(user: User = Depends(auth.get_current_user)):
    """Reprocessa notificações perdidas (GET /missed_feeds) — reconciliação."""
    from . import mercadolivre as ml
    try:
        perdidas = _ml_run(lambda ml: ml.missed_feeds(user.id))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Não foi possível consultar missed_feeds: {e}")
    itens = perdidas if isinstance(perdidas, list) else (perdidas.get("results") or []) if isinstance(perdidas, dict) else []
    reprocessados = 0
    for n in itens[:200]:
        if not isinstance(n, dict):
            continue
        try:
            r = _ml_run(lambda ml: ml.processar_notificacao(user.id, n.get("topic"), n.get("resource")))
            if r.get("ok") and not r.get("erro"):
                reprocessados += 1
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "encontradas": len(itens), "reprocessadas": reprocessados}


# =========================================================================== #
# SAÚDE & MODERAÇÃO — anúncios pausados/em revisão e saúde baixa, com o motivo
# e o que corrigir (reusa o diagnóstico qualidade_ml).
# =========================================================================== #
_SUBSTATUS_LABEL = {
    "under_review": "Em revisão pelo Mercado Livre",
    "pending_documentation": "Documentação pendente",
    "waiting_for_pictures": "Aguardando fotos",
    "picture_download_pending": "Fotos em processamento",
    "suspended": "Suspenso",
    "banned": "Banido por infração",
    "forbidden": "Conteúdo proibido",
    "freeze": "Congelado",
    "deleted": "Excluído",
    "out_of_stock": "Sem estoque",
    "expired": "Expirado",
    "inactive": "Inativo",
    "moderation_reports": "Reportado por moderação",
    "warning": "Advertência de qualidade",
}
_SUBSTATUS_GRAVE = {"suspended", "banned", "forbidden", "pending_documentation", "under_review", "freeze"}


def _substatus_lista(sub):
    if not sub:
        return []
    if isinstance(sub, list):
        return [str(x) for x in sub if x]
    return [p for p in str(sub).split(",") if p]


def _saude_painel(user_id, situacao="todos", busca=None, page=1, page_size=40):
    from .models import MLItemCache
    db = SessionLocal()
    try:
        itens = db.query(MLItemCache).filter(MLItemCache.user_id == user_id).all()
    finally:
        db.close()
    linhas = []
    dist = {"critico": 0, "medio": 0, "bom": 0, "sem": 0}
    for it in itens:
        subs = _substatus_lista(it.sub_status)
        grave = any(s in _SUBSTATUS_GRAVE for s in subs)
        saude_pct = round((it.saude or 0) * 100) if it.saude is not None else None
        if saude_pct is None:
            dist["sem"] += 1
        elif saude_pct < 50:
            dist["critico"] += 1
        elif saude_pct < 80:
            dist["medio"] += 1
        else:
            dist["bom"] += 1
        # classifica em qual balde de atenção cai
        estado = None
        if it.status == "under_review" or grave:
            estado = "revisao"
        elif it.status == "paused":
            estado = "pausado"
        elif it.status == "active" and saude_pct is not None and saude_pct < 80:
            estado = "saude_baixa"
        if estado:
            linhas.append({
                "item_id": it.item_id, "sku": it.sku, "titulo": it.titulo, "imagem": it.imagem,
                "status": it.status, "sub_status": subs,
                "motivos": [_SUBSTATUS_LABEL.get(s, s) for s in subs],
                "grave": grave, "saude": saude_pct, "permalink": it.permalink, "estado": estado,
            })
    kpis = {
        "total": len(itens),
        "revisao": sum(1 for x in linhas if x["estado"] == "revisao"),
        "pausados": sum(1 for x in linhas if x["estado"] == "pausado"),
        "saude_baixa": sum(1 for x in linhas if x["estado"] == "saude_baixa"),
        "atencao": len(linhas),
        "distribuicao": dist,
        "saude_media": (round(sum((it.saude or 0) * 100 for it in itens if it.saude is not None)
                              / max(1, sum(1 for it in itens if it.saude is not None)))
                        if any(it.saude is not None for it in itens) else None),
    }

    def _passa(x):
        if situacao and situacao != "todos" and x["estado"] != situacao:
            return False
        if busca:
            q = busca.strip().lower()
            if q not in f"{x['titulo'] or ''} {x['sku'] or ''} {x['item_id']}".lower():
                return False
        return True

    filtrados = [x for x in linhas if _passa(x)]
    # graves primeiro, depois menor saúde
    filtrados.sort(key=lambda x: (not x["grave"], x["saude"] if x["saude"] is not None else 100))
    total = len(filtrados)
    ini = max(0, (page - 1) * page_size)
    return {"kpis": kpis, "itens": filtrados[ini:ini + page_size], "total": total,
            "situacao": situacao, "page": page, "page_size": page_size}


@app.get("/api/mercadolivre/saude/painel")
def ml_saude_painel(situacao: str = Query("todos"), busca: str = Query(""),
                    page: int = Query(1, ge=1), page_size: int = Query(40, ge=1, le=200),
                    user: User = Depends(auth.get_current_user)):
    if situacao not in ("todos", "revisao", "pausado", "saude_baixa"):
        situacao = "todos"
    return _saude_painel(user.id, situacao=situacao, busca=busca or None, page=page, page_size=page_size)


@app.get("/api/mercadolivre/saude/item/{item_id}")
def ml_saude_item(item_id: str, user: User = Depends(auth.get_current_user)):
    """Diagnóstico do anúncio: status/motivos de moderação + o que corrigir (score e componentes)."""
    diag = _ml_run(lambda ml: ml.qualidade_ml(item_id, user.id))
    subs = _substatus_lista(diag.get("sub_status"))
    grave = any(s in _SUBSTATUS_GRAVE for s in subs)
    return {
        "item_id": item_id, "titulo": diag.get("titulo"), "status": diag.get("status"),
        "sub_status": subs, "motivos": [_SUBSTATUS_LABEL.get(s, s) for s in subs], "grave": grave,
        "score": diag.get("score"), "health": diag.get("health"),
        "componentes": diag.get("componentes") or [],
        "reativavel": (diag.get("status") in ("paused",) and not grave),
    }


@app.get("/api/mercadolivre/agentes/execucoes")
def ml_agentes_execucoes(limit: int = Query(10, ge=1, le=50), user: User = Depends(auth.get_current_user)):
    from .models import AgenteExecucao
    db = SessionLocal()
    try:
        rows = (db.query(AgenteExecucao).filter(AgenteExecucao.user_id == user.id)
                .order_by(AgenteExecucao.quando.desc()).limit(limit).all())
        return {"execucoes": [{"quando": r.quando.isoformat() if r.quando else None, "gatilho": r.gatilho,
                               "aplicados": r.aplicados, "ignorados": r.ignorados, "falhas": r.falhas,
                               "detalhe": r.detalhe or []} for r in rows]}
    finally:
        db.close()


@app.get("/api/mercadolivre/promocoes")
def ml_promocoes(user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.promocoes_do_vendedor(user.id))


@app.get("/api/mercadolivre/promocoes/{item_id}")
def ml_promocoes_item(item_id: str, user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.promocoes_do_item(item_id, user.id))


@app.post("/api/mercadolivre/promocoes/aplicar")
def ml_promo_aplicar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    item_id = payload.get("item_id")
    deal = payload.get("deal_price")
    if not item_id or deal is None:
        raise HTTPException(status_code=422, detail="Informe item_id e deal_price.")
    return _ml_run(lambda ml: ml.aplicar_desconto(item_id, deal, payload.get("top_deal_price"),
                   payload.get("inicio"), payload.get("fim"), user.id))


@app.post("/api/mercadolivre/promocoes/remover")
def ml_promo_remover(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    item_id = payload.get("item_id")
    if not item_id:
        raise HTTPException(status_code=422, detail="Informe item_id.")
    return _ml_run(lambda ml: ml.remover_desconto(item_id, user.id))


# --- Qualidade do anúncio ---
@app.get("/api/mercadolivre/qualidade/{item_id}")
def ml_qualidade(item_id: str, user: User = Depends(auth.get_current_user)):
    return _ml_run(lambda ml: ml.qualidade_ml(item_id, user.id))


# --- Webhook do ML (notificações; sem auth, path fixo p/ cadastrar no app) ---
def _processar_notificacao_ml(uid, topic, resource, ev_id):
    """Processa a notificação em background e atualiza o log (respeita o SLA de 500ms)."""
    from . import mercadolivre as ml
    from .models import MLWebhookEvento
    resultado = None
    ok = False
    try:
        r = ml.processar_notificacao(uid, topic, resource)
        ok = bool(r.get("ok")) and not r.get("erro")
        resultado = ("item " + r["item_id"]) if r.get("item_id") else \
                    ("envio " + r["shipment_id"]) if r.get("shipment_id") else \
                    (r.get("erro") or ("ignorado" if r.get("ignorado") else "ok"))
    except Exception as e:  # noqa: BLE001
        resultado = str(e)[:200]
    if ev_id:
        db = SessionLocal()
        try:
            ev = db.query(MLWebhookEvento).filter_by(id=ev_id).first()
            if ev:
                ev.processado = ok
                ev.resultado = (resultado or "")[:200]
                db.commit()
        finally:
            db.close()


@app.post("/api/mercadolivre/notificacoes")
async def ml_notificacoes(request: Request, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    topic = body.get("topic")
    resource = body.get("resource")
    attempts = body.get("attempts") or 1
    resource_id = str(resource).rstrip("/").split("/")[-1] if resource else None
    # resolve o usuário pelo seller_id
    uid = None
    sid = str(body.get("user_id") or "")
    if sid:
        db = SessionLocal()
        try:
            from .models import MLConta
            c = db.query(MLConta).filter_by(seller_id=sid).first()
            uid = c.user_id if c else None
        finally:
            db.close()
    # registra o evento (rápido) para o painel/auditoria
    ev_id = None
    try:
        from .models import MLWebhookEvento
        db = SessionLocal()
        try:
            ev = MLWebhookEvento(user_id=uid, topic=topic, resource=resource,
                                 resource_id=resource_id, attempts=int(attempts or 1), processado=False)
            db.add(ev)
            db.commit()
            db.refresh(ev)
            ev_id = ev.id
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        pass
    # processa em background e responde 200 já (SLA < 500ms)
    background_tasks.add_task(_processar_notificacao_ml, uid, topic, resource, ev_id)
    return {"ok": True}


# =============================== Central de Atendimento =============================== #
@app.get("/api/atendimento/status")
def atendimento_status(user: User = Depends(auth.get_current_user)):
    from . import mercadolivre as ml
    return ml.status_conexao(user.id)


@app.get("/api/atendimento/stats")
def atendimento_stats(user: User = Depends(auth.get_current_user)):
    from . import atendimento, mercadolivre as ml
    try:
        return atendimento.stats(user.id)
    except ml.MLNaoConfigurado:
        return {"canal": "mercadolivre", "sem_resposta": 0, "respondidas": 0,
                "tempo_medio_min": None, "nao_conectado": True}


@app.get("/api/atendimento/perguntas")
def atendimento_perguntas(status: str = "UNANSWERED", limite: int = 50,
                          user: User = Depends(auth.get_current_user)):
    from . import atendimento, mercadolivre as ml
    try:
        return atendimento.inbox(user.id, status=status, limite=limite)
    except ml.MLNaoConfigurado:
        return {"total": 0, "canal": "mercadolivre", "perguntas": [], "nao_conectado": True}
    except ml.MLErro as e:
        raise HTTPException(status_code=502, detail=f"Mercado Livre: {e}")


@app.post("/api/atendimento/sugerir")
def atendimento_sugerir(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    from . import atendimento
    pergunta = ((payload or {}).get("pergunta") or "").strip()
    produto = (payload or {}).get("produto") or ""
    if not pergunta:
        raise HTTPException(status_code=422, detail="Informe a pergunta.")
    return atendimento.sugerir(user.id, pergunta, produto)


@app.post("/api/atendimento/responder")
def atendimento_responder(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    from . import atendimento, mercadolivre as ml
    qid = (payload or {}).get("question_id")
    texto = ((payload or {}).get("texto") or "").strip()
    if not qid or not texto:
        raise HTTPException(status_code=422, detail="Informe question_id e texto.")
    try:
        return atendimento.responder(user.id, qid, texto)
    except ml.MLNaoConfigurado as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ml.MLErro as e:
        raise HTTPException(status_code=502, detail=f"Mercado Livre: {e}")


@app.post("/api/atendimento/ocultar")
def atendimento_ocultar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    from . import atendimento, mercadolivre as ml
    qid = (payload or {}).get("question_id")
    if not qid:
        raise HTTPException(status_code=422, detail="Informe question_id.")
    try:
        return atendimento.ocultar(user.id, qid)
    except ml.MLErro as e:
        raise HTTPException(status_code=502, detail=f"Mercado Livre: {e}")


@app.get("/api/produtos/{produto_id}/preco_historico")
def produto_preco_historico(produto_id, dias: int = 30, user: User = Depends(auth.get_current_user)):
    """Histórico do Preço Bling por dia (gravado a cada sync). Alimenta o gráfico do cockpit."""
    from .models import ProdutoPrecoSnapshot
    from datetime import date, timedelta
    limite = date.today() - timedelta(days=max(1, min(int(dias or 30), 180)))
    db = SessionLocal()
    try:
        rows = (db.query(ProdutoPrecoSnapshot)
                .filter(ProdutoPrecoSnapshot.user_id == user.id,
                        ProdutoPrecoSnapshot.produto_id == str(produto_id),
                        ProdutoPrecoSnapshot.dia >= limite)
                .order_by(ProdutoPrecoSnapshot.dia.asc()).all())
        pontos = [{"dia": r.dia.isoformat() if r.dia else None, "preco": float(r.preco or 0)} for r in rows]
    finally:
        db.close()
    return {"produto_id": str(produto_id), "pontos": pontos}


@app.get("/api/diagnostico/multiloja/{produto_id}")
def diagnostico_multiloja(produto_id, lojas: str = "", user: User = Depends(auth.get_current_user)):
    """Testa se a API pública do Bling expõe preço por canal via ?idLoja=.
    'lojas' = IDs separados por vírgula. Sem isso, usa as lojas conhecidas desta conta."""
    padrao = ["203414926", "204884434", "203923623", "205946980", "205916963", "205693668"]
    ids = [s.strip() for s in lojas.split(",") if s.strip()] or padrao
    try:
        return bling.probe_multiloja(user.id, produto_id, ids)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/api/produtos/{produto_id}/preco_canal")
def aplicar_preco_canal(produto_id, payload: dict = Body(...),
                        user: User = Depends(auth.get_current_user)):
    """Grava o preço no canal (marketplace) específico, sem tocar no preço-base.
    Body: {id_loja, preco}. Registra o envio para o painel de sincronização."""
    id_loja = payload.get("id_loja")
    preco = payload.get("preco")
    if not id_loja or preco in (None, ""):
        raise HTTPException(status_code=422, detail="Informe id_loja e preco.")
    try:
        res = bling.atualizar_preco_canal(user.id, produto_id, id_loja, float(preco))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Bling recusou a gravação por canal: {e}")
    db = SessionLocal()
    try:
        webhooks.registrar_envio(db, user.id, produto_id, None, [f"preço canal {id_loja}"])
    finally:
        db.close()
    from . import notificacoes as notif
    notif.criar(user.id, "precificacao", f"Preço atualizado: {_brl(preco)}",
                f"Produto {produto_id} no canal {id_loja}.", ok=True,
                modulo="precificacao", entidade_id=produto_id)
    return res


@app.get("/api/produtos/{produto_id}/posicionamento")
def produto_posicionamento(produto_id, canal: str = "mercado_livre",
                           user: User = Depends(auth.get_current_user)):
    """Varredura de posicionamento: acha o produto no canal e classifica o preço vs mercado."""
    try:
        raw = (bling.obter_produto(user.id, produto_id) or {}).get("data", {}) or {}
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    return scraper.posicionamento(raw.get("nome") or "", raw.get("preco") or 0, canal=canal)


@app.get("/api/marketplaces/capacidades")
def marketplaces_capacidades(user: User = Depends(auth.get_current_user)):
    """O que dá pra monitorar em cada marketplace (ML, Shopee, TikTok, Shein) e como ativar."""
    return scraper.capacidades()


@app.get("/api/produtos/{produto_id}/conselho")
def produto_conselho(produto_id, user: User = Depends(auth.get_current_user)):
    """Convoca o Conselho de IA para o produto: falas dos agentes + plano com ações."""
    try:
        return agentes.conselho(user.id, produto_id)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


def _status(margem) -> str:
    """Classifica a margem líquida em faixa de saúde. Usado pelo dashboard e pelo monitoramento.
    Critério alinhado ao pricing.py (saudável a partir de 15%)."""
    m = float(margem or 0)
    if m >= 15:
        return "lucro_ideal"
    if m >= 5:
        return "atencao"
    return "critico"


@app.get("/api/kpis")
def kpis_dashboard(dias: int = 30, user: User = Depends(auth.get_current_user)):
    """KPIs do período: GMV, ticket, venda por canal, mais vendidos, tendência, risco de ruptura."""
    try:
        pedidos = bling.listar_pedidos_periodo(user.id, dias=dias)
        produtos = (bling.listar_produtos(user.id, limite=100).get("data") or [])
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    resultado = {"dias": dias, **kpis.calcular(pedidos, produtos)}
    # traduz o ID da loja para o nome da loja (ex.: 205946980 -> "Shopee NOVO")
    try:
        mapa = bling.lojas_da_conta(user.id)  # {id_loja: {nome, integracao}}
        for c in resultado.get("por_canal", []):
            meta = mapa.get(str(c.get("loja")))
            if meta and meta.get("nome"):
                c["loja"] = meta["nome"]
            elif str(c.get("loja")) == "sem_loja":
                c["loja"] = "Venda direta"
    except Exception:
        pass
    return resultado


@app.get("/api/dashboard/carteira")
def dashboard_carteira(canal: str = "mercadolivre", user: User = Depends(auth.get_current_user)):
    """Margem líquida por produto sobre o catálogo COMPLETO (cache), não a API live.
    Devolve resumo agregado (todos os produtos) + uma amostra de itens para o heatmap/watchlist."""
    produtos = catalogo.todos(user.id)
    cfg = precificacao.obter_config(user.id)
    cont = {"lucro_ideal": 0, "atencao": 0, "critico": 0}
    soma = 0.0
    n = 0
    itens = []
    for p in produtos:
        custo = float(p.get("custo") or 0)
        preco = float(p.get("preco") or 0)
        if preco <= 0:
            continue
        av = precificacao.avaliar_com_cfg(cfg, custo, preco, canal)
        margem = av["margem_atual"] if av["margem_atual"] is not None else (av["margem_sugerida"] or 0)
        st = _status(margem)
        cont[st] = cont.get(st, 0) + 1
        soma += margem
        n += 1
        itens.append({"sku": p.get("sku"), "nome": p.get("nome"), "custo": custo,
                      "preco_atual": preco, "preco_sugerido": av["preco_sugerido"],
                      "margem_liquida": margem, "status": st})
    for it in itens:
        it["pot"] = (round((it["preco_sugerido"] - it["preco_atual"]) / it["preco_atual"] * 100, 1)
                     if it["preco_atual"] and it["preco_sugerido"] else None)
    piores = sorted([i for i in itens if i["status"] != "lucro_ideal"],
                    key=lambda x: x["margem_liquida"])[:6]
    melhores = sorted([i for i in itens if i["pot"] is not None and i["pot"] > 1],
                      key=lambda x: x["pot"], reverse=True)[:6]
    resumo = {"total": n, "ideal": cont["lucro_ideal"], "atencao": cont["atencao"],
              "critico": cont["critico"], "margem_media": round(soma / n, 1) if n else 0,
              "piores": piores, "melhores": melhores, "canal": canal}
    return {"resumo": resumo, "itens": itens[:200]}


@app.get("/api/diagnostico/pedidos")
def diag_pedidos(user: User = Depends(auth.get_current_user)):
    """Payload CRU dos pedidos de venda — revela se a listagem traz itens (mais vendidos)."""
    try:
        return bling.listar_pedidos(user.id, limite=3)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/api/ia/campo")
def ia_campo(payload: dict, user: User = Depends(auth.get_current_user)):
    """Reescreve ou cria o conteúdo de um campo com IA. Body: {campo, texto, instrucao?, nome?}."""
    campo = (payload.get("campo") or "descrição").strip()
    texto = (payload.get("texto") or "").strip()
    instrucao = (payload.get("instrucao") or "").strip()
    nome = (payload.get("nome") or "").strip()
    base = texto if texto else f"(vazio — crie do zero a partir do produto: {nome or 'produto de armarinho'})"
    prompt = (
        "Você é copywriter de e-commerce para marketplaces brasileiros (Mercado Livre, Shopee, Amazon). "
        f"Reescreva o campo '{campo}' do anúncio, otimizando para busca e conversão, mantendo a informação "
        "correta, em HTML simples quando fizer sentido. Responda SOMENTE com o texto final, sem comentários.\n"
        + (f"Instrução do usuário: {instrucao}\n" if instrucao else "")
        + (f"Produto: {nome}\n" if nome else "")
        + f"Conteúdo atual:\n{base}"
    )
    try:
        return {"texto": ai._gerar_texto(user.id, prompt)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"IA indisponível: {e}")


@app.get("/api/diagnostico/sistema")
def diag_sistema(user: User = Depends(auth.get_current_user)):
    """Revela qual banco está em uso (SQLite efêmero x Postgres persistente) e o estado das migrações."""
    from sqlalchemy import text
    from .db import engine
    from .models import OAuthState
    banco = engine.url.get_backend_name()  # 'sqlite' ou 'postgresql'
    ver, n_states = None, -1
    with SessionLocal() as db:
        try:
            ver = db.execute(text("select version_num from alembic_version")).scalar()
        except Exception:  # noqa: BLE001
            pass
        try:
            n_states = db.query(OAuthState).count()
        except Exception:  # noqa: BLE001
            pass
    return {
        "banco": banco,
        "persistente": banco != "sqlite",
        "alerta": None if banco != "sqlite" else
                  "Backend em SQLite efêmero: dados e conexão somem a cada restart. Defina DATABASE_URL para o Postgres.",
        "alembic_versao": ver,
        "oauth_states_no_banco": n_states,
    }


@app.get("/api/diagnostico/produto/{produto_id}")
def diag_produto(produto_id: int, user: User = Depends(auth.get_current_user)):
    """Payload CRU de um produto no Bling — revela os campos reais (fotos, etc.)."""
    try:
        return bling.obter_produto(user.id, produto_id)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/diagnostico/nfe/{nfe_id}")
def diag_nfe(nfe_id: str, user: User = Depends(auth.get_current_user)):
    """Payload CRU de uma NF-e no Bling — revela a estrutura real da nota inteira."""
    try:
        return bling.obter_nfe(user.id, nfe_id)
    except bling.BlingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


# ------------------------------ Precificação ------------------------------ #
@app.post("/api/precificar")
def precificar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Body: {produto, custos_globais, taxas_por_canal?}"""
    return pricing.precificar(
        payload.get("produto", {}),
        payload.get("custos_globais", {}),
        payload.get("taxas_por_canal", {}),
    )


@app.post("/api/precificar/lote")
def precificar_lote(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Precificação em massa.

    Body: {
      custos_globais, taxas_por_canal?, canal: 'mercadolivre'|'shopee',
      itens: [{produto_id, custo, embalagem?, frete?}],
      aplicar: bool   # se true, grava no Bling
    }
    """
    canal = payload.get("canal", "mercadolivre")
    aplicar = bool(payload.get("aplicar", False))
    cfg = precificacao.obter_config(user.id)
    resultados = []
    for item in payload.get("itens", []):
        av = precificacao.avaliar_com_cfg(cfg, float(item.get("custo", 0) or 0), 0, canal)
        preco = av["preco_sugerido"]
        linha = {
            "produto_id": item.get("produto_id"),
            "preco": preco,
            "margem_liquida": av["margem_sugerida"],
            "aplicado": False,
        }
        if aplicar and preco and item.get("produto_id"):
            try:
                bling.atualizar_preco(user.id, int(item["produto_id"]), preco)
                linha["aplicado"] = True
            except Exception as e:  # noqa: BLE001 — registra falha por item, segue o lote
                linha["erro"] = str(e)
        resultados.append(linha)
    if aplicar:
        aplicados = sum(1 for r in resultados if r.get("aplicado"))
        if aplicados:
            from . import notificacoes as notif
            notif.criar(user.id, "precificacao", f"{aplicados} preço(s) reprecificado(s) em lote",
                        f"Canal {canal}. Confira os novos preços.", ok=True, modulo="precificacao")
    return {"canal": canal, "aplicado": aplicar, "itens": resultados}


_LOTE_IA_CAMPOS = {
    "titulo": ("nome", "título do anúncio (curto, com material e medida, para busca)"),
    "nome": ("nome", "título do anúncio (curto, com material e medida, para busca)"),
    "descricao": ("descricaoComplementar", "descrição complementar (rica, para busca e conversão)"),
    "descricao_complementar": ("descricaoComplementar", "descrição complementar (rica, para busca e conversão)"),
}


@app.post("/api/produtos/lote/ia")
def lote_ia(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Gera título OU descrição com IA para vários produtos e (opcional) grava no Bling.
    Body: {produto_ids: [...], campo: 'titulo'|'descricao', aplicar: bool, instrucao?}."""
    ids = payload.get("produto_ids") or []
    campo = (payload.get("campo") or "titulo").strip().lower()
    aplicar = bool(payload.get("aplicar", True))
    instrucao = (payload.get("instrucao") or "").strip()
    if campo not in _LOTE_IA_CAMPOS:
        raise HTTPException(status_code=422, detail="campo deve ser 'titulo' ou 'descricao'.")
    if not ids:
        raise HTTPException(status_code=422, detail="Informe produto_ids.")
    campo_bling, campo_label = _LOTE_IA_CAMPOS[campo]
    ids = ids[:30]  # teto de segurança por lote
    db = SessionLocal()
    resultados = []
    try:
        for pid in ids:
            linha = {"produto_id": pid, "aplicado": False}
            try:
                raw = (bling.obter_produto(user.id, pid) or {}).get("data", {}) or {}
                nome = raw.get("nome") or ""
                atual = raw.get(campo_bling) or ""
                base = atual if atual else f"(vazio — crie do zero a partir do produto: {nome})"
                prompt = (
                    "Você é copywriter de e-commerce para marketplaces brasileiros. "
                    f"Reescreva o campo '{campo_label}' do anúncio, otimizando para busca e conversão, "
                    "mantendo a informação correta. Responda SOMENTE com o texto final, sem comentários.\n"
                    + (f"Instrução: {instrucao}\n" if instrucao else "")
                    + f"Produto: {nome}\nConteúdo atual:\n{base}"
                )
                texto = ai._gerar_texto(user.id, prompt)
                if campo_bling == "nome":
                    texto = " ".join(texto.split())[:150]
                linha["texto"] = texto
                if aplicar:
                    bling.atualizar_produto(user.id, int(pid), {campo_bling: texto})
                    webhooks.registrar_envio(db, user.id, pid, raw.get("codigo"), [campo])
                    linha["aplicado"] = True
            except Exception as e:  # noqa: BLE001 — falha por item não derruba o lote
                linha["erro"] = str(e)[:160]
            resultados.append(linha)
    finally:
        db.close()
    ok = sum(1 for r in resultados if r.get("aplicado"))
    return {"campo": campo, "aplicado": aplicar, "ok": ok, "total": len(resultados), "itens": resultados}
    if margem >= 30:
        return "lucro_ideal"
    if margem >= 15:
        return "atencao"
    return "critico"


def _img_do_payload(dados):
    """Primeira imagem do payload bruto do Bling (lista ou GET)."""
    if not isinstance(dados, dict):
        return None
    ext = (((dados.get("midia") or {}).get("imagens") or {}).get("externas") or [])
    for i in ext:
        if isinstance(i, dict) and i.get("link"):
            return i["link"]
    return dados.get("imagemURL") or None


def _marketplaces_do_payload(dados):
    """Canais onde o produto está anunciado (vinculosLojas, se vier no payload)."""
    if not isinstance(dados, dict):
        return []
    for chave in ("vinculosLojas", "lojas", "produtosLojas"):
        arr = dados.get(chave)
        if isinstance(arr, list) and arr:
            vins = bling.parse_vinculos_multiloja(arr)
            return [{"canal": v.get("canal"), "nome": v.get("nome"), "publicado": bool(v.get("publicado"))}
                    for v in vins if v.get("canal")]
    return []


@app.post("/api/monitoramento")
def monitoramento(payload: dict = Body(default={}),
                  user: User = Depends(auth.get_current_user)):
    """Catálogo no modelo LÍQUIDO. O preço do Bling é o líquido que o lojista quer
    receber. Por canal de marketplace, calcula o preço de LISTA que neta o Preço Bling
    (gross-up pelas taxas daquele canal). Canal 'bling' (padrão) = só o Preço Bling +
    margem real vs custo. Lê do cache local (todos os produtos); se vazio, cai pro
    Bling ao vivo (limitado). Enriquece com imagem, marketplaces e vendas 30d."""
    from .models import ProdutoCache
    canal = payload.get("canal", "bling")
    cfg = precificacao.obter_config(user.id)
    canal_cfg = precificacao._canal_cfg(cfg, canal) if (canal and canal != "bling") else None

    # Preço praticado por canal: líquido que aquele preço de lista neta, e status vs Preço Bling (alvo).
    canais_cfg = {c.get("canal"): c for c in (cfg.get("canais") or []) if c.get("canal")}
    _imp = float(cfg.get("imposto") or 0)
    _emb = float(cfg.get("embalagem") or 0)

    def _status_canal(cn, preco_lista, preco_bling, custo):
        if not preco_lista or preco_lista <= 0:
            return None, None
        ccfg = canais_cfg.get(cn)
        fx = precificacao._faixa_para_preco(ccfg.get("faixas") or [], preco_lista) if (ccfg and ccfg.get("faixas")) else {}
        taxa = preco_lista * (float(fx.get("comissao") or 0) + float(fx.get("fixo_pct") or 0)) / 100.0 + float(fx.get("fixo") or 0)
        liq = round(preco_lista - taxa - preco_lista * _imp / 100.0 - _emb, 2)
        if custo and custo > 0 and liq <= custo:
            flag = "prejuizo"
        elif preco_bling and liq < preco_bling - 0.01:
            flag = "abaixo"
        elif preco_bling and liq > preco_bling + 0.01:
            flag = "acima"
        else:
            flag = "ok"
        return liq, flag

    def _monta(produto_id, sku, nome, preco_bling, custo, estoque, atualizado, imagem=None, marketplaces=None, vendas_un=0):
        margem = round((preco_bling - custo) / preco_bling * 100, 1) if (preco_bling > 0 and custo > 0) else None
        if preco_bling <= 0:
            status = "sem_base"
        elif custo <= 0:
            status = "sem_custo"
        elif preco_bling <= custo:
            status = "critico"            # netando abaixo do custo
        elif margem is not None and margem < 10:
            status = "atencao"
        else:
            status = "lucro_ideal"
        pra_netar = None
        if canal_cfg and preco_bling > 0:
            sug = precificacao.precificar_venda_canal(
                preco_bling, canal_cfg["faixas"], cfg["imposto"], cfg["cartao"], cfg["embalagem"])
            pra_netar = sug["preco"] if sug else None
        return {
            "id": produto_id, "sku": sku, "nome": nome,
            "custo": custo, "estoque": estoque,
            "imagem": imagem, "marketplaces": marketplaces or [], "vendas": vendas_un,
            "preco_bling": preco_bling, "pra_netar": pra_netar,
            "margem_real": margem, "status": status,
            "atualizado": atualizado.isoformat() if atualizado else None,
            # compat com as chaves antigas que o front ainda lê
            "preco_atual": preco_bling, "preco_sugerido": pra_netar, "margem_liquida": margem or 0,
        }

    from sqlalchemy.orm import load_only
    from .models import ShopeeItemCache
    db = SessionLocal()
    try:
        linhas = (db.query(ProdutoCache)
                  .options(load_only(ProdutoCache.produto_id, ProdutoCache.sku, ProdutoCache.nome,
                                     ProdutoCache.imagem, ProdutoCache.preco, ProdutoCache.custo,
                                     ProdutoCache.saldo, ProdutoCache.marketplaces, ProdutoCache.atualizado_em))
                  .filter_by(user_id=user.id).all())
        try:
            shopee_px = {}
            for s in db.query(ShopeeItemCache.sku, ShopeeItemCache.imagem, ShopeeItemCache.preco,
                              ShopeeItemCache.preco_original, ShopeeItemCache.em_promocao,
                              ShopeeItemCache.promo_nome).filter(
                    ShopeeItemCache.user_id == user.id, ShopeeItemCache.sku.isnot(None)).all():
                if s[0]:
                    prat = float(s[3] or 0) or float(s[2] or 0)   # preço cheio (original) ou atual
                    promo = float(s[2] or 0)                      # preço corrente (promo, se houver)
                    shopee_px[s[0]] = {"imagem": s[1], "preco": prat,
                                       "preco_promo": promo if (s[4] and promo and promo < prat) else None,
                                       "em_promocao": bool(s[4]), "promo_nome": s[5]}
            shopee_img = {k: v["imagem"] for k, v in shopee_px.items()}
            shopee_skus = set(shopee_px.keys())
        except Exception:  # noqa: BLE001 — cache da Shopee ainda sem tabela: segue sem badge/imagem Shopee
            db.rollback()
            shopee_skus = set()
            shopee_img = {}
            shopee_px = {}
    finally:
        db.close()

    def _mk(reg_marketplaces, sku, preco_bling=0.0, custo=0.0):
        out = []
        for m in (reg_marketplaces or []):
            if not (isinstance(m, dict) and m.get("canal")):
                continue
            e = dict(m)
            pl = float(e.get("preco") or 0)
            if pl > 0:
                e["liquido"], e["flag"] = _status_canal(e["canal"], pl, preco_bling, custo)
            out.append(e)
        canais = {m.get("canal") for m in out}
        # Shopee: cache direto é a fonte da verdade do preço praticado
        sc = shopee_px.get(sku)
        if sc:
            sh = next((m for m in out if m.get("canal") == "shopee"), None)
            if sh is None:
                sh = {"canal": "shopee", "publicado": True}
                out.append(sh)
            sh["publicado"] = True
            pl = float(sc.get("preco") or 0)
            if pl > 0:
                sh["preco"] = pl
                sh["liquido"], sh["flag"] = _status_canal("shopee", pl, preco_bling, custo)
            if sc.get("em_promocao"):
                sh["promo"] = True
                if sc.get("preco_promo"):
                    sh["preco_promo"] = sc["preco_promo"]
        elif sku in shopee_skus and "shopee" not in canais:
            out.append({"canal": "shopee", "publicado": True})
        return out

    if linhas:
        itens = [_monta(p.produto_id, p.sku, p.nome, float(p.preco or 0), float(p.custo or 0),
                        float(p.saldo or 0), p.atualizado_em, p.imagem or shopee_img.get(p.sku),
                        _mk(p.marketplaces, p.sku, float(p.preco or 0), float(p.custo or 0)), 0) for p in linhas]
        return {"canal": canal, "fonte": "cache", "total": len(itens), "itens": itens}

    # Fallback: cache ainda não sincronizado — lê o Bling ao vivo (limitado).
    try:
        bruto = bling.listar_produtos(user.id, pagina=payload.get("pagina", 1),
                                      limite=payload.get("limite", 100))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
    itens = []
    for p in bruto.get("data", []):
        custo = float(p.get("precoCusto") or (p.get("fornecedor") or {}).get("precoCusto") or 0)
        itens.append(_monta(p.get("id"), p.get("codigo"), p.get("nome"),
                            float(p.get("preco") or 0), custo, 0.0, None,
                            _img_do_payload(p), _mk(None, p.get("codigo"), float(p.get("preco") or 0), custo), 0))
    return {"canal": canal, "fonte": "bling", "total": len(itens), "itens": itens}


@app.get("/api/catalogo/vendas")
def catalogo_vendas(user: User = Depends(auth.get_current_user)):
    """Vendas 30d por SKU (Shopee) — chamada à parte pra não travar a lista do Catálogo."""
    try:
        return {"vendas": shopee.vendas_por_sku(user.id)}
    except Exception:  # noqa: BLE001
        return {"vendas": {}}


@app.post("/api/catalogo/ajustar_precos")
def catalogo_ajustar_precos(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Ajuste em massa do Preço Bling dos produtos selecionados.
    Body: {ids:[produto_id], modo:'pct'|'fixo', direcao:'mais'|'menos', valor:float}.
    Recalcula cada preço-base, grava no Bling e atualiza o cache local."""
    from .models import ProdutoCache
    ids = [str(i) for i in (payload.get("ids") or [])]
    modo = payload.get("modo", "pct")
    direcao = payload.get("direcao", "mais")
    valor = float(payload.get("valor") or 0)
    if not ids or valor <= 0:
        raise HTTPException(status_code=422, detail="Informe os itens e um valor positivo.")
    sinal = 1 if direcao == "mais" else -1
    db = SessionLocal()
    resultados, aplicados = [], 0
    try:
        regs = {r.produto_id: r for r in db.query(ProdutoCache).filter(
            ProdutoCache.user_id == user.id, ProdutoCache.produto_id.in_(ids)).all()}
        for pid in ids:
            reg = regs.get(pid)
            base = float(reg.preco or 0) if reg else 0.0
            if base <= 0:
                resultados.append({"id": pid, "erro": "sem preço-base"})
                continue
            novo = base * (1 + sinal * valor / 100.0) if modo == "pct" else base + sinal * valor
            novo = round(max(novo, 0.01), 2)
            try:
                bling.atualizar_preco(user.id, int(pid), novo)
                if reg:
                    reg.preco = novo
                aplicados += 1
                resultados.append({"id": pid, "de": round(base, 2), "para": novo, "ok": True})
            except Exception as e:  # noqa: BLE001
                resultados.append({"id": pid, "erro": str(e)[:120]})
        db.commit()
    finally:
        db.close()
    if aplicados:
        from . import notificacoes as notif
        notif.criar(user.id, "precificacao", f"{aplicados} preço(s) ajustado(s) em massa",
                    f"Ajuste de Preço Bling aplicado a {aplicados} produto(s).", ok=True, modulo="catalogo")
    return {"aplicados": aplicados, "total": len(ids), "itens": resultados}


# ------------------------------ Concorrência ------------------------------ #
@app.post("/api/concorrencia/preco")
def concorrencia_preco(payload: dict = Body(...),
                       user: User = Depends(auth.get_current_user)):
    """Busca o preço do concorrente e simula o impacto na SUA margem real.

    Body: {url, seletor?, custo_base, canal, taxas_por_canal?, imposto?, cartao?}
    """
    url = payload.get("url")
    if not url:
        raise HTTPException(status_code=422, detail="Informe 'url'.")
    achado = scraper.buscar_preco(url, payload.get("seletor"))
    preco = achado.get("preco")
    resposta = {"preco_concorrente": preco, "fonte": achado.get("fonte")}
    if achado.get("erro"):
        resposta["erro"] = achado["erro"]

    if preco:
        canal = payload.get("canal", "mercadolivre")
        cfg = pricing.PLATAFORMAS.get(canal, {})
        taxas = (payload.get("taxas_por_canal") or {}).get(canal, {})
        sim = pricing.simular_concorrente(
            preco,
            float(payload.get("custo_base", 0)),
            taxas.get("comissao", cfg.get("comissao", 0)),
            taxas.get("fixo", cfg.get("fixo", 0)),
            float(payload.get("imposto", 0)),
            float(payload.get("cartao", 0)),
        )
        resposta["simulacao"] = sim
    return resposta


@app.post("/api/concorrencia/precos")
def concorrencia_precos(payload: dict = Body(...),
                        user: User = Depends(auth.get_current_user)):
    """Radar multi-concorrentes: lista de URLs -> preço + margem real de cada um.

    Body: {urls: [..], custo_base, canal, taxas_por_canal?, imposto?, cartao?}
    """
    urls = payload.get("urls") or []
    if not isinstance(urls, list) or not urls:
        raise HTTPException(status_code=422, detail="Informe uma lista 'urls'.")
    canal = payload.get("canal", "mercadolivre")
    cfg = pricing.PLATAFORMAS.get(canal, {})
    taxas = (payload.get("taxas_por_canal") or {}).get(canal, {})
    base = float(payload.get("custo_base", 0))
    imp = float(payload.get("imposto", 0))
    cartao = float(payload.get("cartao", 0))

    resultados = []
    for achado in scraper.buscar_precos(urls):
        linha = {"url": achado.get("url"), "preco_concorrente": achado.get("preco"),
                 "fonte": achado.get("fonte")}
        if achado.get("erro"):
            linha["erro"] = achado["erro"]
        if achado.get("preco"):
            linha["simulacao"] = pricing.simular_concorrente(
                achado["preco"], base,
                taxas.get("comissao", cfg.get("comissao", 0)),
                taxas.get("fixo", cfg.get("fixo", 0)), imp, cartao)
        resultados.append(linha)
    return {"canal": canal, "resultados": resultados}


@app.post("/api/decisao/preco")
def decisao_preco(payload: dict = Body(...),
                  user: User = Depends(auth.get_current_user)):
    """Motor de decisão (Fase 4): dado o preço dos concorrentes + custo + piso de
    margem, decide subir/baixar/manter — nunca abaixo da viabilidade.

    Body: {custo_base, preco_atual, precos_concorrentes:[..], canal, comissao?, fixo?,
           imposto?, cartao?, piso_margem?, estrategia?, delta?, delta_tipo?, passo_min?}
    """
    try:
        return decisao.decidir_preco(
            custo_base=float(payload.get("custo_base", 0)),
            preco_atual=float(payload.get("preco_atual", 0)),
            precos_concorrentes=payload.get("precos_concorrentes") or [],
            canal=payload.get("canal", "mercadolivre"),
            comissao=payload.get("comissao"),
            fixo=payload.get("fixo"),
            imposto=float(payload.get("imposto", 0)),
            cartao=float(payload.get("cartao", 0)),
            piso_margem=float(payload.get("piso_margem", 15)),
            estrategia=payload.get("estrategia", "match"),
            delta=float(payload.get("delta", 1)),
            delta_tipo=payload.get("delta_tipo", "pct"),
            passo_min=float(payload.get("passo_min", 0.5)),
        )
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"Parâmetros inválidos: {e}")


@app.post("/api/precificar/reverso")
def precificar_reverso(payload: dict = Body(...),
                       user: User = Depends(auth.get_current_user)):
    """Markup reverso por canal: dado custo + margem alvo, devolve o preço de venda
    em cada marketplace e o Raio-X em R$ (taxa do canal, impostos/cartão, lucro).

    Body: {custo_base, imposto?, cartao?, margem_alvo?, taxas_por_canal?}
    """
    try:
        return pricing.precificar_reverso(
            custo_base=float(payload.get("custo_base", 0)),
            imposto_pct=float(payload.get("imposto", 0)),
            cartao_pct=float(payload.get("cartao", 0)),
            margem_alvo=float(payload.get("margem_alvo", 30)),
            taxas_por_canal=payload.get("taxas_por_canal"),
        )
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"Parâmetros inválidos: {e}")


@app.post("/api/qualidade/cadastro")
def qualidade_cadastro(payload: dict = Body(...),
                       user: User = Depends(auth.get_current_user)):
    """Score 0-100 de completude da ficha (Insight Engine).

    Body: {nome, ean, ncm, peso, descricao|descricao_longa}
    """
    return qualidade.score_cadastro(payload)


# --------------------------------- IA ------------------------------------- #
@app.post("/api/ia/descricao")
def ia_descricao(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    nome = payload.get("nome_produto")
    if not nome:
        raise HTTPException(status_code=422, detail="Informe 'nome_produto'.")
    texto = ai.gerar_descricao(user.id, nome, payload.get("caracteristicas", ""),
                               blindar=bool(payload.get("blindar", False)),
                               modelo=payload.get("model"))
    return {"descricao_gerada": texto}


@app.post("/api/ia/sac")
def ia_sac(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Resposta de atendimento humanizada (tom Sóstrass)."""
    texto = ai.gerar_sac(user.id, payload.get("relato", ""), modelo=payload.get("model"))
    return {"resposta": texto}


@app.post("/api/estudio/imagem")
def estudio_imagem(payload: dict = Body(...),
                   user: User = Depends(auth.get_current_user)):
    """Estúdio criativo: gera foto de produto (Gemini). Devolve imagem em base64.

    Vídeo (Veo) não tem endpoint aqui — é assíncrono/caro e fica para um fluxo próprio.
    """
    return ai.gerar_imagem(user.id, payload.get("prompt", ""),
                           payload.get("negativo", ""), payload.get("modelo"))


# ------------------------------- Agentes ---------------------------------- #
@app.get("/api/agentes")
def agentes_listar(user: User = Depends(auth.get_current_user)):
    """Lista os agentes disponíveis e as ferramentas de cada um."""
    return {"agentes": agentes.listar()}


@app.post("/api/agentes/{agente}/mensagem")
def agentes_mensagem(agente: str, payload: dict = Body(...),
                     user: User = Depends(auth.get_current_user)):
    """Conversa com um agente. Body: {mensagem, historico?:[{autor,texto}]}.

    Os agentes propõem e calculam via ferramentas determinísticas — não alteram
    preço no canal nem nota no Bling.
    """
    return agentes.conversar(user.id, agente, payload.get("mensagem", ""),
                             historico=payload.get("historico"))


# ================= Precificação por canal (faixas de preço) =============== #
@app.get("/api/precificacao/config")
def precificacao_get_config(user: User = Depends(auth.get_current_user)):
    """Custos globais + taxas por canal com faixas (cria padrão na primeira vez)."""
    return precificacao.obter_config(user.id)


@app.put("/api/precificacao/config")
def precificacao_put_config(payload: dict = Body(...),
                            user: User = Depends(auth.get_current_user)):
    """Salva custos globais e/ou canais. Body: {imposto?, cartao?, embalagem?, frete?,
    margem_padrao?, canais?:[{canal,nome,ativo,faixas:[{ate,comissao,fixo,fixo_pct}]}]}"""
    return precificacao.salvar_config(user.id, payload)


@app.post("/api/precificacao/restaurar")
def precificacao_restaurar(user: User = Depends(auth.get_current_user)):
    """Restaura os padrões pesquisados (sobrescreve a config atual)."""
    return precificacao.restaurar_padrao(user.id)


@app.post("/api/precificacao/calcular")
def precificacao_calcular(payload: dict = Body(...),
                          user: User = Depends(auth.get_current_user)):
    """Preço sugerido por canal a partir do custo. Body: {custo, margem?, apenas_ativos?}"""
    if payload.get("custo") is None:
        raise HTTPException(status_code=422, detail="Informe 'custo'.")
    return precificacao.precificar(user.id, float(payload["custo"]),
                                   margem=payload.get("margem"),
                                   apenas_ativos=payload.get("apenas_ativos", True))


# ======================= Radar (histórico de mercado) ===================== #
@app.post("/api/radar/alvos")
def radar_add_alvo(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Cadastra um anúncio de concorrente para monitorar. Body: {sku, url, nome?, marketplace?}"""
    sku = payload.get("sku")
    url = payload.get("url")
    if not sku or not url:
        raise HTTPException(status_code=422, detail="Informe 'sku' e 'url'.")
    return radar.adicionar_alvo(user.id, sku, url, payload.get("nome"),
                                payload.get("marketplace"))


@app.get("/api/radar/alvos")
def radar_list_alvos(sku: str | None = None, user: User = Depends(auth.get_current_user)):
    return {"alvos": radar.listar_alvos(user.id, sku)}


@app.post("/api/radar/manual")
def radar_add_manual(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Cadastra um concorrente digitado na mão e já grava o preço. Body: {sku, nome, preco, marketplace?}"""
    sku = payload.get("sku")
    nome = (payload.get("nome") or "").strip()
    preco = payload.get("preco")
    if not sku or not nome or preco in (None, ""):
        raise HTTPException(status_code=422, detail="Informe 'sku', 'nome' e 'preco'.")
    try:
        p = float(str(preco).replace(",", "."))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="Preço inválido.")
    if p <= 0:
        raise HTTPException(status_code=422, detail="Preço deve ser maior que zero.")
    return radar.adicionar_manual(user.id, sku, nome, p, payload.get("marketplace") or "shopee")


@app.delete("/api/radar/alvos/{alvo_id}")
def radar_del_alvo(alvo_id: int, user: User = Depends(auth.get_current_user)):
    if not radar.remover_alvo(user.id, alvo_id):
        raise HTTPException(status_code=404, detail="Alvo não encontrado.")
    return {"removido": True}


@app.post("/api/radar/snapshot")
def radar_snapshot(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Registra manualmente uma foto de preço. Body: {alvo_id, preco_oferta?, preco_normal?}"""
    alvo_id = payload.get("alvo_id")
    if alvo_id is None:
        raise HTTPException(status_code=422, detail="Informe 'alvo_id'.")
    r = radar.registrar_snapshot(user.id, int(alvo_id),
                                 preco_oferta=payload.get("preco_oferta"),
                                 preco_normal=payload.get("preco_normal"))
    if not r.get("ok"):
        raise HTTPException(status_code=404, detail=r.get("erro"))
    return r


@app.post("/api/radar/varrer")
def radar_varrer(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Roda o scraper em todos os alvos do SKU e guarda os snapshots. Body: {sku}"""
    sku = payload.get("sku")
    if not sku:
        raise HTTPException(status_code=422, detail="Informe 'sku'.")
    return radar.varrer(user.id, sku)


@app.post("/api/radar/varrer-tudo")
def radar_varrer_tudo(user: User = Depends(auth.get_current_user)):
    """Varre de uma vez todos os SKUs com alvos ativos do usuário."""
    return radar.varrer_usuario(user.id)


@app.get("/api/radar/alertas")
def radar_alertas(dias: int = 7, sku: str | None = None, limiar_pct: float = 5.0,
                  user: User = Depends(auth.get_current_user)):
    """Feed de mudanças que pedem ação (quedas, altas, novos mínimos) a partir dos snapshots."""
    return radar.alertas(user.id, dias=dias, limiar_pct=limiar_pct, sku=sku)


@app.get("/api/radar/historico")
def radar_historico(sku: str, dias: int = 7, user: User = Depends(auth.get_current_user)):
    """Série por concorrente + estatísticas (menor/maior/moda/último) do período."""
    return radar.historico(user.id, sku, dias)


@app.post("/api/radar/recomendacao")
def radar_recomendacao(payload: dict = Body(...),
                       user: User = Depends(auth.get_current_user)):
    """Radar + motor de decisão: recomendação de preço por concorrente (e geral).

    Body: {sku, custo_base, preco_atual, canal?, comissao?, fixo?, imposto?,
           cartao?, piso_margem?, estrategia?, delta?, delta_tipo?}
    """
    if (not payload.get("sku") or payload.get("custo_base") is None
            or payload.get("preco_atual") is None):
        raise HTTPException(status_code=422,
                            detail="Informe 'sku', 'custo_base' e 'preco_atual'.")
    return radar.recomendar(
        user.id, payload["sku"],
        custo_base=float(payload["custo_base"]), preco_atual=float(payload["preco_atual"]),
        canal=payload.get("canal", "mercadolivre"),
        comissao=payload.get("comissao"), fixo=payload.get("fixo"),
        imposto=float(payload.get("imposto", 0)), cartao=float(payload.get("cartao", 0)),
        piso_margem=float(payload.get("piso_margem", 15)),
        estrategia=payload.get("estrategia", "match"),
        delta=float(payload.get("delta", 1)), delta_tipo=payload.get("delta_tipo", "pct"),
    )


# ============================ Nota Fiscal (NF-e) =========================== #
def _nfe_cfg(user_id: int) -> NfeConfig:
    """Pega (ou cria) a config de NF-e do tenant."""
    with SessionLocal() as db:
        cfg = db.query(NfeConfig).filter(NfeConfig.user_id == user_id).first()
        if cfg is None:
            cfg = NfeConfig(user_id=user_id)
            db.add(cfg)
            db.commit()
            db.refresh(cfg)
        db.expunge(cfg)
        return cfg


def _cfg_dict(cfg: NfeConfig) -> dict:
    return {"auto": bool(cfg.auto), "desconto_tipo": cfg.desconto_tipo,
            "desconto_valor": cfg.desconto_valor, "remover_frete": bool(cfg.remover_frete),
            "situacao_pendente": cfg.situacao_pendente,
            "desconto_plataformas": getattr(cfg, "desconto_plataformas", None) or {}}


@app.get("/api/nfe/config")
def nfe_get_config(user: User = Depends(auth.get_current_user)):
    return _cfg_dict(_nfe_cfg(user.id))


@app.put("/api/nfe/config")
def nfe_set_config(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Liga/desliga o modo automático e define a regra padrão (desconto + frete)."""
    with SessionLocal() as db:
        cfg = db.query(NfeConfig).filter(NfeConfig.user_id == user.id).first()
        if cfg is None:
            cfg = NfeConfig(user_id=user.id)
            db.add(cfg)
        if "auto" in payload:
            cfg.auto = bool(payload["auto"])
        if "desconto_tipo" in payload and payload["desconto_tipo"] in ("percentual", "valor"):
            cfg.desconto_tipo = payload["desconto_tipo"]
        if "desconto_valor" in payload:
            cfg.desconto_valor = float(payload["desconto_valor"])
        if "remover_frete" in payload:
            cfg.remover_frete = bool(payload["remover_frete"])
        if "situacao_pendente" in payload:
            cfg.situacao_pendente = int(payload["situacao_pendente"])
        if "desconto_plataformas" in payload and isinstance(payload["desconto_plataformas"], dict):
            # sanitiza: só plataformas conhecidas, tipo válido e valor numérico
            limpo = {}
            for plat, regra in payload["desconto_plataformas"].items():
                if not isinstance(regra, dict):
                    continue
                tipo = regra.get("tipo")
                if tipo not in ("percentual", "valor"):
                    continue
                try:
                    valor = float(regra.get("valor") or 0)
                except (TypeError, ValueError):
                    continue
                if valor > 0:
                    limpo[str(plat)] = {"tipo": tipo, "valor": valor}
            cfg.desconto_plataformas = limpo
        db.commit()
        db.refresh(cfg)
        return _cfg_dict(cfg)


@app.get("/api/nfe/pendentes")
def nfe_pendentes(pagina: int = 1, limite: int = 100,
                  situacao: int | None = None,
                  user: User = Depends(auth.get_current_user)):
    """Lista as NF-e pendentes (editáveis), já normalizadas para a UI (número, cliente,
    valor, situação, editável). Usa o código de situação da config por padrão."""
    cfg = _nfe_cfg(user.id)
    sit = situacao if situacao is not None else cfg.situacao_pendente
    try:
        raw = bling.listar_nfe(user.id, pagina=pagina, limite=limite, situacao=sit)
        # lista rápida: sem buscar nota a nota (os valores vêm em background via /api/nfe/valores)
        return {"notas": nfe.resumir_lista(raw), "situacao": sit}
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/nfe/pendentes/todas")
def nfe_pendentes_todas(situacao: int | None = None, max_paginas: int = 80,
                        user: User = Depends(auth.get_current_user)):
    """Lista TODAS as NF-e da situação, paginando o Bling até esgotar (sem o limite de 100).
    A lista é leve (sem valor/UF — esses vêm do /valores em lote). Teto de páginas por segurança.

    Categorias: situacao=6 => Autorizadas (situação 5 E 6, pois a conta pode usar qualquer uma);
    situacao=0 => Todas (mescla as situações usuais, já que o Bling desta conta não lista sem filtro);
    None => padrão (pendentes); demais => filtra por aquela situação."""
    import time as _t
    cfg = _nfe_cfg(user.id)
    if situacao == 0:
        sits = [cfg.situacao_pendente, 5, 6, 4, 2, 7]   # Todas (mescla)
    elif situacao == 6:
        sits = [5, 6]                                    # Autorizadas (5 ou 6)
    elif situacao is not None:
        sits = [situacao]
    else:
        sits = [cfg.situacao_pendente]
    # remove duplicatas preservando ordem
    sits = list(dict.fromkeys([s for s in sits if s is not None]))

    todas, vistos = [], set()
    try:
        for s in sits:
            pagina = 1
            while pagina <= max_paginas:
                raw = bling.listar_nfe(user.id, pagina=pagina, limite=100, situacao=s)
                lote = nfe.resumir_lista(raw)
                if not lote:
                    break
                for n in lote:
                    nid = n.get("id")
                    if nid not in vistos:
                        vistos.add(nid)
                        todas.append(n)
                if len(lote) < 100:
                    break
                pagina += 1
                _t.sleep(0.2)  # respeita o rate limit do Bling
        return {"notas": todas, "situacao": situacao, "situacoes": sits, "total": len(todas)}
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/api/nfe/simular")
def nfe_simular(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Revisão pura (sem tocar no Bling): recalcula desconto/frete sobre os itens.

    Body: {itens:[{indice,descricao,quantidade,valor_unitario}], frete,
           desconto_tipo, desconto_valor, descontos_por_item?, remover_frete}
    """
    return nfe.aplicar_edicao(
        payload.get("itens", []),
        desconto_tipo=payload.get("desconto_tipo", "percentual"),
        desconto_valor=float(payload.get("desconto_valor", 0)),
        descontos_por_item=payload.get("descontos_por_item"),
        remover_frete=bool(payload.get("remover_frete", True)),
        frete_atual=float(payload.get("frete", 0)),
    )


@app.post("/api/nfe/auto/processar")
def nfe_auto_processar(user: User = Depends(auth.get_current_user)):
    """Roda o modo automático agora: aplica a regra padrão em todas as pendentes."""
    cfg = _nfe_cfg(user.id)
    if not cfg.auto:
        raise HTTPException(status_code=409, detail="Modo automático desligado. Ligue em /api/nfe/config.")
    try:
        return nfe.processar_automatico(user.id, cfg)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/api/nfe/valores")
def nfe_valores(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Busca em lote os dados que a lista do Bling não traz: valor, plataforma, UF, tributos
    (IBPT), pedido, chave, links e motivo de rejeição. Chamado em background pela tela.

    Faz as chamadas em PARALELO (cada bling.obter_nfe abre sua própria sessão de DB e respeita
    o rate-limit) — sem isso, dezenas de notas demoravam segundos e a tela ficava em 0,00.
    """
    from concurrent.futures import ThreadPoolExecutor
    ids = [str(i) for i in (payload.get("ids") or [])][:100]
    if not ids:
        return {}
    lojas_map = nfe._mapear_lojas_plataforma(user.id)  # cache 1h — resolve NuvemShop/site próprio

    def _um(nid):
        try:
            return nid, nfe.resumo_extra_raw(bling.obter_nfe(user.id, nid), lojas_map)
        except Exception:  # noqa: BLE001
            return nid, {"valor": 0, "plataforma": None}

    out = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for nid, dados in ex.map(_um, ids):
            out[nid] = dados
    return out


@app.post("/api/nfe/aplicar-selecionadas")
def nfe_aplicar_selecionadas(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Aplica o desconto padrão em uma lista ESPECÍFICA de notas (seleção em massa) e salva no Bling."""
    ids = payload.get("ids") or []
    if not ids:
        raise HTTPException(status_code=400, detail="Nenhuma nota selecionada.")
    cfg = _nfe_cfg(user.id)
    try:
        r = nfe.processar_ids(user.id, ids, cfg)
        from . import notificacoes as notif
        notif.criar(user.id, "nfe", f"Desconto aplicado em {r.get('aplicadas', 0)} nota(s)",
                    "Seleção em massa concluída. Venha conferir e transmitir no Bling.",
                    ok=(r.get("aplicadas", 0) > 0), modulo="nfe")
        return r
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/nfe/faturamento")
def nfe_faturamento(user: User = Depends(auth.get_current_user)):
    """Monitor do teto do Simples: RBT12, total do ano, projeção e % do teto/sublimite
    (estimativa por amostragem — a lista do Bling não traz valor). Lê o último snapshot."""
    return nfe.resumo_faturamento(user.id)


@app.post("/api/nfe/faturamento/recalcular")
def nfe_faturamento_recalcular(background: BackgroundTasks,
                               user: User = Depends(auth.get_current_user)):
    """Dispara o recálculo do faturamento em background (job pesado). A tela busca o
    resultado depois via GET /api/nfe/faturamento."""
    background.add_task(nfe.recalcular_faturamento, user.id)
    return {"iniciado": True, "mensagem": "Recálculo iniciado. Pode levar alguns minutos; atualize em instantes."}


@app.get("/api/nfe/contagens")
def nfe_contagens(user: User = Depends(auth.get_current_user)):
    """Contagem de notas por situação (para as abas). Paginação leve da lista resumida."""
    try:
        return nfe.contagens_situacao(user.id)
    except Exception:
        return {"pendentes": 0, "rejeitadas": 0, "autorizadas": 0, "canceladas": 0, "todas": 0, "aproximado": False}


@app.get("/api/nfe/pedidos-sem-nfe")
def nfe_pedidos_sem_nfe(dias: int = 30, user: User = Depends(auth.get_current_user)):
    """Pedidos de venda do Bling (todos os canais) sem NF-e emitida no período."""
    try:
        return nfe.pedidos_sem_nfe(user.id, dias=dias)
    except Exception as e:
        return {"dias": dias, "total": 0, "valor_total": 0, "pedidos": [], "erro": str(e)}


@app.post("/api/nfe/aplicar-todas")
def nfe_aplicar_todas(user: User = Depends(auth.get_current_user)):
    """Ação MANUAL: aplica o desconto padrão em TODAS as notas pendentes de uma vez e
    devolve cada uma ao Bling (sem precisar editar nota por nota no painel do Bling).
    Não depende do modo automático estar ligado. Retorna um relatório por nota."""
    cfg = _nfe_cfg(user.id)
    try:
        r = nfe.processar_automatico(user.id, cfg)
        from . import notificacoes as notif
        if r.get("aplicadas", 0) > 0:
            notif.criar(user.id, "nfe", f"Desconto aplicado em {r.get('aplicadas', 0)} nota(s) pendentes",
                        "Aplicação em lote concluída. Venha conferir e transmitir no Bling.",
                        ok=True, modulo="nfe")
        return r
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/notificacoes")
def notificacoes(limite: int = 40, user: User = Depends(auth.get_current_user)):
    """Centro de notificações da plataforma: eventos do Bling (NF-e, produtos, pedidos…) +
    o que os módulos fizeram (agentes aplicaram desconto, avaliações respondidas, radar de
    concorrência…), em ordem cronológica."""
    from . import notificacoes as notif
    itens = list(notif.listar(user.id, limite))  # notificações próprias (app)
    try:
        db = SessionLocal()
        try:
            regs = (db.query(WebhookEvento)
                    .filter(WebhookEvento.user_id == user.id)
                    .order_by(WebhookEvento.id.desc())
                    .limit(max(1, min(limite, 100))).all())
            for e in regs:
                cat, titulo, texto, ok = webhooks.descrever_evento(e.recurso, e.acao, e.resultado)
                itens.append({
                    "id": f"w{e.id}", "categoria": cat, "titulo": titulo, "texto": texto, "ok": ok,
                    "recurso": e.recurso, "acao": e.acao, "entidade_id": e.entidade_id,
                    "quando": e.recebido_em.isoformat() if e.recebido_em else None,
                    "resultado": e.resultado,
                })
        finally:
            db.close()
    except Exception:  # noqa: BLE001 — schema de webhook desatualizado não pode derrubar o sino
        pass
    # ordena por horário desc (quando ausente vai pro fim) e corta no limite
    itens.sort(key=lambda x: (x.get("quando") or ""), reverse=True)
    return itens[:max(1, min(limite, 100))]


@app.post("/api/notificacoes/marcar-lidas")
def notificacoes_marcar_lidas(user: User = Depends(auth.get_current_user)):
    from . import notificacoes as notif
    return {"lidas": notif.marcar_lidas(user.id)}


@app.post("/api/notificacoes/arquivar")
def notificacoes_arquivar(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Arquiva (marca como lidas) notificações específicas. Body: {ids:[...]} ou {id:'n123'}."""
    from . import notificacoes as notif
    ids = payload.get("ids")
    if ids is None and payload.get("id") is not None:
        ids = [payload["id"]]
    return {"arquivadas": notif.arquivar(user.id, ids or [])}


@app.get("/api/nfe/eventos")
def nfe_eventos(limite: int = 25, user: User = Depends(auth.get_current_user)):
    """Últimos eventos de NF-e recebidos do Bling via webhook, com o resultado do
    auto-apply do desconto (o que o sistema fez com cada nota)."""
    recursos = ["nfe", "notafiscal", "nota_fiscal", "notafiscaleletronica"]
    from sqlalchemy import func
    db = SessionLocal()
    try:
        regs = (db.query(WebhookEvento)
                .filter(WebhookEvento.user_id == user.id,
                        func.lower(WebhookEvento.recurso).in_(recursos))
                .order_by(WebhookEvento.id.desc())
                .limit(max(1, min(limite, 100))).all())
        return [{
            "id": e.id, "evento": e.event, "acao": e.acao, "entidade_id": e.entidade_id,
            "quando": e.recebido_em.isoformat() if e.recebido_em else None,
            "resultado": e.resultado,
        } for e in regs]
    finally:
        db.close()


@app.get("/api/nfe/{nfe_id}")
def nfe_obter(nfe_id: str, user: User = Depends(auth.get_current_user)):
    """Detalhe normalizado de uma nota (itens + frete) para o editor de desconto."""
    try:
        return nfe.normalizar_nfe(bling.obter_nfe(user.id, nfe_id),
                                  nfe._mapear_lojas_plataforma(user.id))
    except bling.BlingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/nfe/{nfe_id}/completa")
def nfe_completa(nfe_id: str, user: User = Depends(auth.get_current_user)):
    """Visão COMPLETA da nota: emitente, destinatário, totais, impostos, transporte, itens e links."""
    try:
        det = nfe.detalhar_nfe(bling.obter_nfe(user.id, nfe_id),
                               nfe._mapear_lojas_plataforma(user.id))
        # Emitente (sua empresa) — reaproveita os dados do cadastro de impressão (etiqueta/folha).
        try:
            imp = shopee_impressao.obter_config(user.id) or {}
            nome = (imp.get("emitente_nome") or "").strip()
            det["emitente"] = {
                "nome": nome,
                "cnpj": (imp.get("emitente_cnpj") or "").strip(),
                "endereco": (imp.get("emitente_endereco") or "").strip(),
                "cidade": (imp.get("emitente_cidade") or "").strip(),
            } if nome or imp.get("emitente_cnpj") else None
        except Exception:
            det["emitente"] = None
        return det
    except bling.BlingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/nfe/{nfe_id}/diagnostico-edicao")
def nfe_diagnostico_edicao(nfe_id: str, user: User = Depends(auth.get_current_user)):
    """Dry-run: mostra a estrutura real da nota e o payload que seria enviado (sem enviar),
    para ver as parcelas e checar se a soma bate com o total."""
    cfg = _nfe_cfg(user.id)
    try:
        return nfe.diagnosticar_edicao(
            user.id, nfe_id,
            desconto_tipo=cfg.desconto_tipo, desconto_valor=float(cfg.desconto_valor or 0),
            remover_frete=bool(cfg.remover_frete),
        )
    except bling.BlingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/api/nfe/{nfe_id}/conciliacao-shopee")
def nfe_conciliacao_shopee(nfe_id: str, user: User = Depends(auth.get_current_user)):
    """Compara o valor fiscal da NF-e com o repasse real da Shopee (escrow) do pedido."""
    try:
        return nfe.conciliar_shopee(user.id, nfe_id)
    except bling.BlingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/api/nfe/conciliacao-shopee-lote")
def nfe_conciliacao_shopee_lote(payload: dict = Body(...), user: User = Depends(auth.get_current_user)):
    """Concilia várias notas Shopee de uma vez (resumo + divergências)."""
    ids = payload.get("ids") or []
    if not ids:
        raise HTTPException(status_code=400, detail="Nenhuma nota informada.")
    try:
        return nfe.conciliar_shopee_lote(user.id, ids)
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/api/nfe/{nfe_id}/enviar")
def nfe_enviar(nfe_id: str, user: User = Depends(auth.get_current_user)):
    """Retransmite uma NF-e ao Sefaz (para reprocessar notas rejeitadas já corrigidas)."""
    try:
        resultado = bling.enviar_nfe(user.id, nfe_id)
        from . import notificacoes as notif
        notif.criar(user.id, "nfe", f"Nota {nfe_id} retransmitida ao Sefaz",
                    "Acompanhe a autorização na lista/no Bling.", ok=True,
                    modulo="nfe", entidade_id=nfe_id)
        return {"ok": True, "resultado": resultado}
    except bling.BlingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except bling.BlingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.post("/api/nfe/{nfe_id}/aplicar")
def nfe_aplicar(nfe_id: str, payload: dict = Body(...),
                user: User = Depends(auth.get_current_user)):
    """Aplica a edição numa nota. enviar=false revisa; enviar=true devolve ao Bling.

    Body: {desconto_tipo, desconto_valor, descontos_por_item?, remover_frete, enviar}
    """
    try:
        res = nfe.editar_nota(
            user.id, nfe_id,
            desconto_tipo=payload.get("desconto_tipo", "percentual"),
            desconto_valor=float(payload.get("desconto_valor", 0)),
            descontos_por_item=payload.get("descontos_por_item"),
            remover_frete=bool(payload.get("remover_frete", True)),
            enviar=bool(payload.get("enviar", False)),
        )
        if res.get("enviado"):
            from . import notificacoes as notif
            total = (res.get("resumo") or {}).get("total_nota")
            notif.criar(user.id, "nfe", f"Desconto aplicado na nota {res.get('numero') or nfe_id}",
                        f"Novo total {_brl(total)}. Confira e transmita no Bling." if total is not None else "Confira a nota.",
                        ok=True, modulo="nfe", entidade_id=nfe_id)
        return res
    except bling.BlingNotFound as e:
        raise HTTPException(status_code=404, detail=str(e))
    except bling.BlingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except bling.BlingAuthError as e:
        raise HTTPException(status_code=401, detail=str(e))
