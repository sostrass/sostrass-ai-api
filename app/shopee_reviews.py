"""IA de avaliações da Shopee — respostas no padrão da loja.

Dois modos:
- manual: a IA sugere um rascunho, você revisa/edita e envia.
- auto: o agente responde sozinho as notas configuradas (ex.: só 4 e 5 estrelas),
  deixando as notas baixas para você responder com cuidado.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from . import ai, shopee
from .db import SessionLocal
from .models import ShopeeReviewConfig, ShopeeReviewLog

# Estado vivo do agente, em memória (por usuário) — alimenta o painel "Agente em ação".
# O disparo roda no mesmo processo (BackgroundTasks/agendador), então o painel lê isto via /atividade.
_PROGRESSO: dict[int, dict] = {}
_CONTAGEM: dict[int, dict] = {}   # cache de contagem (caro: pagina a Shopee)
_CONTANDO: set[int] = set()       # usuários com contagem em andamento
_LOCK = threading.Lock()


def _prog(user_id: int) -> dict:
    return _PROGRESSO.get(user_id) or {"em_andamento": False, "processados": 0, "alvo": 0,
                                        "inicio": None, "fim": None, "ultimo": None}


def _set_prog(user_id: int, **campos):
    with _LOCK:
        cur = dict(_prog(user_id))
        cur.update(campos)
        _PROGRESSO[user_id] = cur


def _registrar_log(user_id: int, comment_id, nota, buyer, produto, texto, modo="auto"):
    db = SessionLocal()
    try:
        db.add(ShopeeReviewLog(user_id=user_id, comment_id=str(comment_id or ""), nota=int(nota or 0),
                               buyer=(buyer or "")[:80], produto=(produto or "")[:120],
                               trecho=(texto or "")[:140], modo=modo))
        db.commit()
    except Exception:  # noqa: BLE001 — log nunca derruba o fluxo
        db.rollback()
    finally:
        db.close()


def historico_log(user_id: int, limite: int = 20) -> list:
    db = SessionLocal()
    try:
        rows = (db.query(ShopeeReviewLog).filter_by(user_id=user_id)
                .order_by(ShopeeReviewLog.criado_em.desc()).limit(limite).all())
        return [{"comment_id": r.comment_id, "nota": r.nota, "buyer": r.buyer,
                 "produto": r.produto, "trecho": r.trecho, "modo": r.modo,
                 "quando": r.criado_em.isoformat() if r.criado_em else None} for r in rows]
    finally:
        db.close()


def contar_avaliacoes(user_id: int, max_paginas: int = 60, forcar: bool = False) -> dict:
    """Conta respondidas x pendentes paginando a Shopee (caro). Cacheia por ~10 min.
    parcial=True se bateu o teto de páginas (loja com muitas avaliações)."""
    cache = _CONTAGEM.get(user_id)
    anterior_pend = cache.get("pendentes") if cache else None
    if cache and not forcar:
        idade = (datetime.utcnow() - cache["_ts"]).total_seconds()
        if idade < 600:
            return {k: v for k, v in cache.items() if k != "_ts"}

    respondidas, pendentes, total = 0, 0, 0
    cursor, parcial = "", False
    _CONTANDO.add(user_id)
    try:
        for i in range(max_paginas):
            try:
                r = shopee.listar_avaliacoes(user_id, cursor=cursor, limite=100, status="ALL")
            except shopee.ShopeeError:
                break
            resp = r.get("response") or {}
            lst = resp.get("item_comment_list") or []
            for c in lst:
                total += 1
                if (c.get("comment_reply") or {}).get("reply"):
                    respondidas += 1
                else:
                    pendentes += 1
            cursor = resp.get("next_cursor") or ""
            if not resp.get("more") or not cursor:
                break
            if i == max_paginas - 1:
                parcial = True
            time.sleep(0.15)  # respiro entre páginas
    finally:
        _CONTANDO.discard(user_id)

    out = {"total": total, "respondidas": respondidas, "pendentes": pendentes, "parcial": parcial}
    _CONTAGEM[user_id] = {**out, "_ts": datetime.utcnow()}
    # avisa quando surgem avaliações novas para responder (sem repetir o mesmo número)
    if anterior_pend is not None and pendentes > anterior_pend:
        try:
            from . import notificacoes as notif
            novas = pendentes - anterior_pend
            notif.criar(user_id, "avaliacao",
                        f"{novas} avaliação(ões) nova(s) para responder",
                        f"Total de {pendentes} pendente(s) na Shopee. Rode o agente para responder.",
                        ok=True, modulo="avaliacoes")
        except Exception:  # noqa: BLE001
            pass
    return out


def atividade(user_id: int) -> dict:
    """Tudo que o painel precisa: progresso vivo + feed das últimas respostas + contagem (cacheada)."""
    cache = _CONTAGEM.get(user_id)
    contagem = ({k: v for k, v in cache.items() if k != "_ts"} if cache else None)
    return {"progresso": _prog(user_id), "log": historico_log(user_id, 15),
            "contagem": contagem, "contando": user_id in _CONTANDO}

TONS = {
    "caloroso": "caloroso, afetuoso e genuíno, como quem gosta de verdade de atender bem",
    "profissional": "profissional, educado e objetivo, transmitindo confiança e seriedade",
    "descontraido": "descontraído e simpático, leve e próximo, mas sem perder o respeito",
}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _config(db, user_id: int) -> ShopeeReviewConfig:
    c = db.query(ShopeeReviewConfig).filter_by(user_id=user_id).first()
    if not c:
        c = ShopeeReviewConfig(user_id=user_id)
        db.add(c)
        db.commit()
        db.refresh(c)
    return c


def _serializar(c: ShopeeReviewConfig) -> dict:
    return {
        "modo": c.modo or "manual",
        "tom": c.tom or "caloroso",
        "limite_chars": c.limite_chars or 450,
        "assinatura": c.assinatura or "",
        "saudacao": c.saudacao or "",
        "instrucoes": c.instrucoes or "",
        "oferecer_chat": bool(c.oferecer_chat),
        "usar_nome": bool(c.usar_nome),
        "usar_emoji": bool(c.usar_emoji),
        "auto_estrelas": list(c.auto_estrelas or [4, 5]),
        "auto_pausa_seg": c.auto_pausa_seg if c.auto_pausa_seg is not None else 5,
        "auto_max_ciclo": c.auto_max_ciclo if c.auto_max_ciclo is not None else 40,
        "emoji_intensidade": getattr(c, "emoji_intensidade", None) or "leve",
        "instrucoes_elogio": getattr(c, "instrucoes_elogio", None) or "",
        "instrucoes_morna": getattr(c, "instrucoes_morna", None) or "",
        "instrucoes_critica": getattr(c, "instrucoes_critica", None) or "",
        "frases_casa": list(getattr(c, "frases_casa", None) or []),
        "frases_proibidas": list(getattr(c, "frases_proibidas", None) or []),
        "cupom_ativo": bool(getattr(c, "cupom_ativo", False)),
        "cupom_codigo": getattr(c, "cupom_codigo", None) or "",
        "cupom_quando": getattr(c, "cupom_quando", None) or "vips",
    }


def obter_config(user_id: int) -> dict:
    db = SessionLocal()
    try:
        return _serializar(_config(db, user_id))
    finally:
        db.close()


def salvar_config(user_id: int, payload: dict) -> dict:
    from datetime import datetime
    db = SessionLocal()
    try:
        c = _config(db, user_id)
        if "modo" in payload and payload["modo"] in ("manual", "auto"):
            c.modo = payload["modo"]
        if "tom" in payload and payload["tom"] in TONS:
            c.tom = payload["tom"]
        if "limite_chars" in payload:
            c.limite_chars = max(120, min(int(payload["limite_chars"]), 1000))
        if "assinatura" in payload:
            c.assinatura = str(payload["assinatura"])[:80]
        if "saudacao" in payload:
            c.saudacao = str(payload["saudacao"])[:80]
        if "instrucoes" in payload:
            c.instrucoes = str(payload["instrucoes"])[:1200]
        if "oferecer_chat" in payload:
            c.oferecer_chat = bool(payload["oferecer_chat"])
        if "usar_nome" in payload:
            c.usar_nome = bool(payload["usar_nome"])
        if "usar_emoji" in payload:
            c.usar_emoji = bool(payload["usar_emoji"])
        if "auto_estrelas" in payload:
            try:
                est = sorted({int(x) for x in payload["auto_estrelas"] if 1 <= int(x) <= 5})
                c.auto_estrelas = est or [4, 5]
            except Exception:  # noqa: BLE001
                pass
        if "auto_pausa_seg" in payload:
            try:
                c.auto_pausa_seg = max(0, min(int(payload["auto_pausa_seg"]), 60))
            except Exception:  # noqa: BLE001
                pass
        if "auto_max_ciclo" in payload:
            try:
                c.auto_max_ciclo = max(1, min(int(payload["auto_max_ciclo"]), 100))
            except Exception:  # noqa: BLE001
                pass
        if "emoji_intensidade" in payload and payload["emoji_intensidade"] in ("nenhum", "leve", "animado"):
            c.emoji_intensidade = payload["emoji_intensidade"]
        for campo in ("instrucoes_elogio", "instrucoes_morna", "instrucoes_critica"):
            if campo in payload:
                setattr(c, campo, str(payload[campo])[:600])
        for campo in ("frases_casa", "frases_proibidas"):
            if campo in payload and isinstance(payload[campo], list):
                setattr(c, campo, [str(x)[:120] for x in payload[campo] if str(x).strip()][:20])
        if "cupom_ativo" in payload:
            c.cupom_ativo = bool(payload["cupom_ativo"])
        if "cupom_codigo" in payload:
            c.cupom_codigo = str(payload["cupom_codigo"])[:40]
        if "cupom_quando" in payload and payload["cupom_quando"] in ("vips", "todas5", "nunca"):
            c.cupom_quando = payload["cupom_quando"]
        c.atualizado_em = datetime.utcnow()
        db.commit()
        return _serializar(c)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Prompt no padrão da loja
# --------------------------------------------------------------------------- #
def montar_prompt(cfg, nota, comentario, produto=None, nome=None, tom=None, vip=False) -> str:
    tom_desc = TONS.get(tom or cfg.tom or "caloroso", TONS["caloroso"])
    coment = (comentario or "").strip() or "(o cliente não escreveu texto, deu só a nota)"
    nota = int(nota or 5)

    partes = [
        "Você é o dono de uma loja de armarinho e aviamentos no Brasil (miçangas, pérolas, strass, "
        "caixas organizadoras, materiais para bijuteria e artesanato) respondendo PUBLICAMENTE a uma "
        "avaliação de cliente na Shopee.",
        f"Tom da resposta: {tom_desc}.",
        f"Nota recebida: {nota}/5.",
    ]
    if produto:
        partes.append(f'Produto avaliado: "{produto}".')
    if nome and cfg.usar_nome:
        partes.append(f'Nome do cliente: "{nome}".')
    partes.append(f'O que o cliente escreveu: "{coment}".')

    regras = [
        f"Escreva em português do Brasil, com no máximo {cfg.limite_chars or 450} caracteres.",
        "Soe humano e específico ao que o cliente disse — proibido resposta genérica de robô.",
        "Não invente fatos nem prometa o que não pode cumprir.",
    ]
    if cfg.usar_nome and nome:
        regras.append("Pode cumprimentar pelo primeiro nome do cliente.")
    elif not cfg.usar_nome:
        regras.append("Não use o nome do cliente.")
    # intensidade de emoji
    intens = getattr(cfg, "emoji_intensidade", None) or "leve"
    if not cfg.usar_emoji or intens == "nenhum":
        regras.append("Não use nenhum emoji.")
    elif intens == "animado":
        regras.append("Pode usar 2 a 3 emojis para dar um tom bem animado e caloroso.")
    else:
        regras.append("Use no máximo 1 emoji leve, só se combinar.")

    # estratégia por faixa de nota (instruções específicas da loja por nota)
    if nota <= 2:
        faixa = (getattr(cfg, "instrucoes_critica", None) or "").strip()
        regras.append("A nota é uma CRÍTICA: peça desculpas com sinceridade, assuma a resolução "
                      "(reposição/troca) e mostre que se importa de verdade. Nunca seja defensivo, "
                      "nunca culpe o cliente e nunca discuta em público.")
        if faixa:
            regras.append(f"Estratégia da loja para críticas: {faixa}")
        if cfg.oferecer_chat:
            regras.append("Convide o cliente a chamar no chat da Shopee para resolver rápido.")
    elif nota == 3:
        faixa = (getattr(cfg, "instrucoes_morna", None) or "").strip()
        regras.append("A nota é MORNA (3★): agradeça o retorno, pergunte com leveza o que faltou "
                      "para ser 5★ e mostre que a opinião muda a loja de verdade.")
        if faixa:
            regras.append(f"Estratégia da loja para notas mornas: {faixa}")
        if cfg.oferecer_chat:
            regras.append("Se fizer sentido, ofereça resolver pelo chat da Shopee.")
    else:
        faixa = (getattr(cfg, "instrucoes_elogio", None) or "").strip()
        regras.append("A nota é um ELOGIO: agradeça com calor, celebre o projeto da cliente "
                      "(bijuteria/artesanato) e convide a voltar / conferir os lançamentos.")
        if faixa:
            regras.append(f"Estratégia da loja para elogios: {faixa}")

    # guard-rails: frases da casa (pode usar) e frases proibidas (bloqueio duro)
    casa = [str(x).strip() for x in (getattr(cfg, "frases_casa", None) or []) if str(x).strip()]
    if casa:
        regras.append("Você pode usar naturalmente, quando couber (sem forçar), frases da casa como: "
                      + "; ".join(f'"{f}"' for f in casa[:8]) + ".")
    proib = [str(x).strip() for x in (getattr(cfg, "frases_proibidas", None) or []) if str(x).strip()]
    if proib:
        regras.append("JAMAIS diga nada parecido com (proibido): "
                      + "; ".join(f'"{f}"' for f in proib[:8]) + ".")

    # cupom de recompra (nunca em críticas, pra não parecer suborno)
    cupom_ativo = bool(getattr(cfg, "cupom_ativo", False))
    cupom_cod = (getattr(cfg, "cupom_codigo", None) or "").strip()
    cupom_quando = getattr(cfg, "cupom_quando", None) or "vips"
    if cupom_ativo and cupom_cod and cupom_quando != "nunca" and nota >= 4:
        cita = (cupom_quando == "todas5" and nota == 5) or (cupom_quando == "vips" and vip)
        if cita:
            regras.append(f'Ofereça com naturalidade o cupom de recompra "{cupom_cod}" como um mimo '
                          "para a próxima compra — nunca soe como suborno, é só um carinho.")

    if cfg.saudacao:
        regras.append(f'Comece a resposta com uma saudação no estilo: "{cfg.saudacao.strip()}".')
    if (cfg.instrucoes or "").strip():
        regras.append(f"Regras específicas da loja que você DEVE respeitar: {cfg.instrucoes.strip()}")
    if (cfg.assinatura or "").strip():
        regras.append(f'Finalize assinando exatamente como: "{cfg.assinatura.strip()}".')

    partes.append("Regras: " + " ".join(f"({i + 1}) {r}" for i, r in enumerate(regras)))
    partes.append("Responda SÓ com o texto final da resposta — sem aspas, sem rótulos, sem explicação.")
    return "\n".join(partes)


# --------------------------------------------------------------------------- #
# Sugerir (rascunho, sem enviar) e enviar
# --------------------------------------------------------------------------- #
def sugerir(user_id: int, nota, comentario, produto=None, nome=None, tom=None, vip=False) -> str:
    db = SessionLocal()
    try:
        cfg = _config(db, user_id)
    finally:
        db.close()
    prompt = montar_prompt(cfg, nota, comentario, produto, nome, tom, vip=vip)
    texto = (ai._gerar_texto(user_id, prompt) or "").strip().strip('"')
    return texto


def enviar(user_id: int, comment_id, texto: str) -> dict:
    return shopee.responder_avaliacao(user_id, comment_id, texto)


# --------------------------------------------------------------------------- #
# Mutirão — responde a fila INTEIRA de pendentes (notas-alvo), com pausa e progresso
# --------------------------------------------------------------------------- #
_PARAR: set[int] = set()   # usuários que pediram para interromper o agente


def parar_agente(user_id: int) -> dict:
    """Pede para o agente interromper o mutirão em andamento (no próximo item)."""
    _PARAR.add(user_id)
    return {"acao": "parando"}


def iniciar_mutirao(user_id: int, completo: bool = False) -> dict:
    """Dispara o agente para responder TODA a fila pendente (notas-alvo), em uma thread
    de fundo, com pausa entre cada resposta e progresso ao vivo. Não bloqueia a requisição.
    completo=True: varre TODOS os produtos um a um para alcançar avaliações antigas
    (a busca global da Shopee só devolve ~1.000 mais recentes)."""
    if _prog(user_id).get("em_andamento"):
        return {"acao": "ja_rodando", "msg": "O agente já está respondendo agora."}
    _PARAR.discard(user_id)
    _set_prog(user_id, em_andamento=True, fase="descobrindo", processados=0, alvo=0,
              inicio=datetime.utcnow().isoformat(), fim=None, ultimo=None, completo=bool(completo))
    t = threading.Thread(target=_rodar_mutirao, args=(user_id, bool(completo)), daemon=True)
    t.start()
    modo = "varredura completa (todos os produtos)" if completo else "as avaliações recentes"
    return {"acao": "iniciado",
            "mensagem": f"O agente começou a responder {modo} em segundo plano, "
                        "com pausa entre cada resposta. Acompanhe o progresso aqui."}


def _rodar_mutirao(user_id: int, completo: bool = False):
    """Fase 1: descobre todos os pendentes-alvo paginando a Shopee (rápido).
    Fase 2: responde um por um, com pausa, atualizando o progresso. Respeita 'parar'."""
    db = SessionLocal()
    try:
        cfg = _config(db, user_id)
        alvos = set(cfg.auto_estrelas or [4, 5])
        pausa = max(int(cfg.auto_pausa_seg if cfg.auto_pausa_seg is not None else 5), 0)
        cfg_snap = ShopeeReviewConfig(
            modo=cfg.modo, tom=cfg.tom, limite_chars=cfg.limite_chars,
            assinatura=cfg.assinatura, saudacao=cfg.saudacao, instrucoes=cfg.instrucoes,
            oferecer_chat=cfg.oferecer_chat, usar_nome=cfg.usar_nome, usar_emoji=cfg.usar_emoji,
        )
    finally:
        db.close()

    pendentes, respondidos, falhas = [], 0, 0
    vistos = set()  # comment_id já coletados (evita duplicar entre passada global e por produto)

    def _coletar(coments):
        for c in coments:
            cid = c.get("comment_id")
            if cid in vistos:
                continue
            if (c.get("comment_reply") or {}).get("reply"):
                continue
            if c.get("rating_star") in alvos:
                vistos.add(cid)
                pendentes.append(c)

    try:
        # FASE 1a — passada GLOBAL (rápida): pega as ~1.000 avaliações mais recentes
        cursor = ""
        for _ in range(15):
            if user_id in _PARAR:
                break
            try:
                resp = shopee.comentarios_brutos(user_id, status="UNANSWERED", cursor=cursor, limite=100)
            except shopee.ShopeeError:
                break
            _lote = resp.get("item_comment_list") or []
            _coletar(_lote)
            try:   # espelha no cache: o que for respondido some da varredura para sempre
                from .db import SessionLocal as _SL2
                from . import shopee_avaliacoes as _av2
                _d2 = _SL2()
                try:
                    for _c in _lote:
                        _av2._gravar(_d2, user_id, _c)
                    _d2.commit()
                finally:
                    _d2.close()
            except Exception:  # noqa: BLE001
                pass
            _set_prog(user_id, fase="descobrindo", alvo=len(pendentes))
            cursor = resp.get("next_cursor") or ""
            if not resp.get("more") or not cursor:
                break
            time.sleep(0.15)

        # FASE 1b — varredura POR PRODUTO. ERA O MAIOR CONSUMIDOR DA API: com ~5.240 SKUs,
        # varria um get_comment por produto a cada ciclo. Agora só roda na PRIMEIRA carga,
        # quando o cache de avaliações ainda está vazio — depois disso o banco cobre o histórico.
        _cache_vazio = True
        try:
            from .db import SessionLocal as _SL
            from . import shopee_avaliacoes as _av
            _d = _SL()
            try:
                _cache_vazio = _av.estado(_d, user_id).get("total_no_cache", 0) == 0
            finally:
                _d.close()
        except Exception:  # noqa: BLE001
            pass
        if completo and _cache_vazio and user_id not in _PARAR:
            try:
                item_ids = shopee.todos_item_ids(user_id)
            except shopee.ShopeeError:
                item_ids = []
            total_prod = len(item_ids)
            for idx, iid in enumerate(item_ids):
                if user_id in _PARAR:
                    break
                cur2 = ""
                for _ in range(20):  # por produto raramente passa de 1 página
                    try:
                        resp = shopee.comentarios_brutos(user_id, item_id=iid, status="UNANSWERED",
                                                         cursor=cur2, limite=100)
                    except shopee.ShopeeError:
                        break
                    _coletar(resp.get("item_comment_list") or [])
                    cur2 = resp.get("next_cursor") or ""
                    if not resp.get("more") or not cur2:
                        break
                if idx % 10 == 0 or idx == total_prod - 1:
                    _set_prog(user_id, fase="varrendo_produtos", alvo=len(pendentes),
                              prod_atual=idx + 1, prod_total=total_prod)
                time.sleep(0.06)  # respeita o limite de chamadas da Shopee

        # enriquece a fila com nome do produto, em lotes (para o prompt da IA)
        try:
            ids_unicos = list({c.get("item_id") for c in pendentes if c.get("item_id")})
            meta = shopee.nomes_itens(user_id, ids_unicos) if ids_unicos else {}
            for c in pendentes:
                c["produto_nome"] = (meta.get(c.get("item_id")) or {}).get("nome")
        except shopee.ShopeeError:
            pass

        # filtra pelo cache: nunca tenta o que já foi respondido nem o que travou em 3 falhas
        try:
            from .db import SessionLocal as _SL5
            from . import shopee_avaliacoes as _av5
            from .models import ShopeeAvaliacaoCache as _AC
            _d5 = _SL5()
            try:
                bloqueados = {r[0] for r in _d5.query(_AC.comment_id).filter(
                    _AC.user_id == user_id,
                    (_AC.respondida == True) | (_AC.tentativas >= _av5.MAX_TENTATIVAS)).all()}  # noqa: E712
                antes_filtro = len(pendentes)
                pendentes = [c for c in pendentes if str(c.get("comment_id")) not in bloqueados]
                if antes_filtro != len(pendentes):
                    print(f"[reviews] {antes_filtro - len(pendentes)} avaliação(ões) já respondidas foram puladas", flush=True)
            finally:
                _d5.close()
        except Exception:  # noqa: BLE001
            pass

        _set_prog(user_id, fase="respondendo", alvo=len(pendentes), processados=0)

        # FASE 2 — responder cada um, espaçado
        for c in pendentes:
            if user_id in _PARAR:
                break
            nota = c.get("rating_star")
            buyer = c.get("buyer_username")
            produto = c.get("produto_nome") or ""
            prompt = montar_prompt(cfg_snap, nota, c.get("comment"), produto or None, buyer, None)
            try:
                texto = (ai._gerar_texto(user_id, prompt) or "").strip().strip('"')
                if not texto:
                    continue
                shopee.responder_avaliacao(user_id, c.get("comment_id"), texto)
                respondidos += 1
                try:   # marca como respondida: sai da varredura definitivamente
                    from .db import SessionLocal as _SL3
                    from . import shopee_avaliacoes as _av3
                    _d3 = _SL3()
                    try: _av3.marcar_respondida(_d3, user_id, c.get("comment_id"), texto)
                    finally: _d3.close()
                except Exception:  # noqa: BLE001
                    pass
                _registrar_log(user_id, c.get("comment_id"), nota, buyer, produto, texto, "auto")
                _set_prog(user_id, processados=respondidos,
                          ultimo={"nota": nota, "buyer": buyer, "produto": produto,
                                  "quando": datetime.utcnow().isoformat()})
                cache = _CONTAGEM.get(user_id)
                if cache:
                    cache["respondidas"] = cache.get("respondidas", 0) + 1
                    cache["pendentes"] = max(0, cache.get("pendentes", 0) - 1)
                if pausa:
                    time.sleep(pausa)
            except Exception as _e:  # noqa: BLE001 — um erro não derruba o mutirão
                falhas += 1
                try:   # conta a tentativa; após 3, aquela avaliação sai da fila
                    from .db import SessionLocal as _SL4
                    from . import shopee_avaliacoes as _av4
                    _d4 = _SL4()
                    try: _av4.marcar_falha(_d4, user_id, c.get("comment_id"), str(_e))
                    finally: _d4.close()
                except Exception:  # noqa: BLE001
                    pass
                continue
    finally:
        interrompido = user_id in _PARAR
        _PARAR.discard(user_id)
        _set_prog(user_id, em_andamento=False, fase="ocioso",
                  fim=datetime.utcnow().isoformat(),
                  resumo={"respondidos": respondidos, "falhas": falhas,
                          "total_fila": len(pendentes), "interrompido": interrompido})
        if respondidos > 0:
            try:
                from . import notificacoes as notif
                notif.criar(user_id, "avaliacao",
                            f"Agente respondeu {respondidos} avaliação(ões)",
                            ("Mutirão interrompido. " if interrompido else "Mutirão concluído. ")
                            + (f"{falhas} falha(s)." if falhas else "Sem falhas."),
                            ok=(falhas == 0), modulo="avaliacoes")
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# Modo automático — o agente responde sozinho as notas configuradas
# --------------------------------------------------------------------------- #
def auto_responder(user_id: int, limite: int = 100) -> dict:
    db = SessionLocal()
    try:
        cfg = _config(db, user_id)
        modo = cfg.modo
        alvos = set(cfg.auto_estrelas or [4, 5])
        pausa = max(int(cfg.auto_pausa_seg or 5), 0)      # segundos entre respostas (anti-flood)
        max_ciclo = max(int(cfg.auto_max_ciclo or 40), 1)  # teto de respostas por ciclo
        cfg_snap = ShopeeReviewConfig(
            modo=cfg.modo, tom=cfg.tom, limite_chars=cfg.limite_chars,
            assinatura=cfg.assinatura, saudacao=cfg.saudacao, instrucoes=cfg.instrucoes,
            oferecer_chat=cfg.oferecer_chat, usar_nome=cfg.usar_nome, usar_emoji=cfg.usar_emoji,
        )
    finally:
        db.close()
    if modo != "auto":
        return {"acao": "manual", "respondidos": 0}

    try:
        r = shopee.listar_avaliacoes(user_id, status="UNANSWERED", limite=limite)
    except shopee.ShopeeError as e:
        return {"acao": "erro", "erro": str(e), "respondidos": 0}

    comentarios = (r.get("response") or {}).get("item_comment_list") or []
    # fila real deste ciclo: sem resposta, nota dentro do alvo, respeitando o teto
    fila = [c for c in comentarios
            if not (c.get("comment_reply") or {}).get("reply") and c.get("rating_star") in alvos]
    alvo = min(len(fila), max_ciclo)
    ignorados = sum(1 for c in comentarios
                    if not (c.get("comment_reply") or {}).get("reply") and c.get("rating_star") not in alvos)

    _set_prog(user_id, em_andamento=True, processados=0, alvo=alvo,
              inicio=datetime.utcnow().isoformat(), fim=None, ultimo=None)

    respondidos, falhas = 0, 0
    atingiu_teto = len(fila) > max_ciclo
    try:
        for c in fila[:max_ciclo]:
            nota = c.get("rating_star")
            buyer = c.get("buyer_username")
            produto = c.get("produto_nome") or ""
            prompt = montar_prompt(cfg_snap, nota, c.get("comment"), produto or None, buyer, None)
            try:
                texto = (ai._gerar_texto(user_id, prompt) or "").strip().strip('"')
                if not texto:
                    continue
                shopee.responder_avaliacao(user_id, c.get("comment_id"), texto)
                respondidos += 1
                _registrar_log(user_id, c.get("comment_id"), nota, buyer, produto, texto, "auto")
                _set_prog(user_id, processados=respondidos,
                          ultimo={"nota": nota, "buyer": buyer, "produto": produto,
                                  "quando": datetime.utcnow().isoformat()})
                if pausa:                   # espaça as chamadas pra não floodar a API da Shopee
                    time.sleep(pausa)
            except Exception:  # noqa: BLE001 — um erro num comentário não derruba o lote
                falhas += 1
                continue
    finally:
        _set_prog(user_id, em_andamento=False, fim=datetime.utcnow().isoformat())
        # atualiza a contagem cacheada (desconta o que acabou de responder)
        cache = _CONTAGEM.get(user_id)
        if cache and respondidos:
            cache["respondidas"] = cache.get("respondidas", 0) + respondidos
            cache["pendentes"] = max(0, cache.get("pendentes", 0) - respondidos)
        if respondidos > 0:
            try:
                from . import notificacoes as notif
                notif.criar(user_id, "avaliacao",
                            f"{respondidos} avaliação(ões) respondida(s) automaticamente",
                            "O agente automático respondeu novas avaliações na Shopee.",
                            ok=True, modulo="avaliacoes")
            except Exception:  # noqa: BLE001
                pass

    return {"acao": "auto", "respondidos": respondidos,
            "ignorados_para_revisao": ignorados, "vistos": len(comentarios),
            "falhas": falhas, "restantes_proximo_ciclo": atingiu_teto}
