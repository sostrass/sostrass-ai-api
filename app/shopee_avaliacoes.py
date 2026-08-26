"""Cache de avaliações Shopee — varre só o que ainda NÃO foi respondido.

O QUE MUDA
Antes: `_coletar` pedia `comment_status="ALL"` e paginava até 30 páginas de 100,
com cache de apenas 15 minutos. Resultado: 17.934 chamadas/mês (54,9% de todo o uso
da API) relendo sempre as mesmas avaliações já respondidas.

Agora:
  · A varredura pede `comment_status=UNANSWERED` — a própria Shopee filtra.
  · Tudo que chega é gravado em `shopee_avaliacao_cache`.
  · Assim que uma resposta é aceita, a avaliação é marcada `respondida=True` e
    NUNCA MAIS é buscada nem tentada de novo.
  · Telas de reputação leem do banco.

EFEITO COLATERAL IMPORTANTE
As 245 falhas de `reply_comment` (7,8%) eram, muito provavelmente, tentativas de
responder avaliações que já tinham resposta. Marcando no banco, isso desaparece.

`shopee.py` é FROZEN — este módulo apenas o consome.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import shopee
from .models import ShopeeAvaliacaoCache

MAX_TENTATIVAS = 3          # depois disso, não insiste mais naquela avaliação


def _texto_resposta(c: dict) -> str:
    rep = c.get("comment_reply") or {}
    return (rep.get("reply") or "").strip()


def _gravar(db, user_id: int, c: dict) -> ShopeeAvaliacaoCache:
    cid = str(c.get("comment_id") or "")
    if not cid:
        return None
    linha = (db.query(ShopeeAvaliacaoCache)
             .filter(ShopeeAvaliacaoCache.user_id == user_id,
                     ShopeeAvaliacaoCache.comment_id == cid).first())
    if linha is None:
        linha = ShopeeAvaliacaoCache(user_id=user_id, comment_id=cid)
        db.add(linha)
    linha.item_id = str(c.get("item_id") or "") or linha.item_id
    linha.order_sn = c.get("order_sn") or linha.order_sn
    linha.rating = c.get("rating_star") or linha.rating
    linha.comentario = c.get("comment") or linha.comentario
    linha.comprador = c.get("buyer_username") or linha.comprador
    linha.tem_midia = bool((c.get("media") or {}).get("image_url_list")
                           or (c.get("media") or {}).get("video_url_list"))
    linha.create_time = c.get("create_time") or linha.create_time
    resp = _texto_resposta(c)
    if resp:
        linha.respondida = True
        linha.resposta = resp
        if not linha.respondida_em:
            linha.respondida_em = datetime.utcnow()
    linha.payload = c
    linha.visto_em = datetime.utcnow()
    return linha


def varrer_pendentes(db, user_id: int, max_paginas: int = 30) -> dict:
    """Busca APENAS as não respondidas (comment_status=UNANSWERED) e grava no cache.
    Poucas páginas bastam: o normal é ter dezenas pendentes, não milhares."""
    chamadas = novas = 0
    cursor = ""
    for _ in range(max_paginas):
        try:
            r = shopee.comentarios_brutos(user_id, status="UNANSWERED",
                                          cursor=cursor, limite=100)
            chamadas += 1
        except Exception as e:  # noqa: BLE001
            print(f"[avaliacoes] varredura falhou: {e}", flush=True)
            break
        lote = r.get("item_comment_list") or []
        for c in lote:
            antes = (db.query(ShopeeAvaliacaoCache)
                     .filter(ShopeeAvaliacaoCache.user_id == user_id,
                             ShopeeAvaliacaoCache.comment_id == str(c.get("comment_id") or "")).count())
            _gravar(db, user_id, c)
            if not antes:
                novas += 1
        db.commit()
        if not r.get("more") or not r.get("next_cursor"):
            break
        cursor = r.get("next_cursor")
    return {"ok": True, "chamadas_api": chamadas, "novas": novas,
            "pendentes": pendentes_qtd(db, user_id)}


def pendentes_qtd(db, user_id: int) -> int:
    return (db.query(ShopeeAvaliacaoCache)
            .filter(ShopeeAvaliacaoCache.user_id == user_id,
                    ShopeeAvaliacaoCache.respondida == False,  # noqa: E712
                    ShopeeAvaliacaoCache.tentativas < MAX_TENTATIVAS).count())


def pendentes(db, user_id: int, limite: int = 60, estrelas: list = None) -> list:
    """As avaliações que ainda precisam de resposta — direto do banco, ZERO API."""
    q = (db.query(ShopeeAvaliacaoCache)
         .filter(ShopeeAvaliacaoCache.user_id == user_id,
                 ShopeeAvaliacaoCache.respondida == False,  # noqa: E712
                 ShopeeAvaliacaoCache.tentativas < MAX_TENTATIVAS))
    if estrelas:
        q = q.filter(ShopeeAvaliacaoCache.rating.in_([int(e) for e in estrelas]))
    linhas = q.order_by(ShopeeAvaliacaoCache.create_time.desc()).limit(limite).all()
    return [{
        "comment_id": l.comment_id, "item_id": l.item_id, "order_sn": l.order_sn,
        "rating": l.rating, "comentario": l.comentario, "comprador": l.comprador,
        "tem_midia": l.tem_midia, "create_time": l.create_time,
        "tentativas": l.tentativas, "payload": l.payload,
    } for l in linhas]


def marcar_respondida(db, user_id: int, comment_id: str, resposta: str) -> None:
    """Chamada depois que a Shopee ACEITA a resposta. A partir daqui a avaliação
    some da varredura para sempre."""
    l = (db.query(ShopeeAvaliacaoCache)
         .filter(ShopeeAvaliacaoCache.user_id == user_id,
                 ShopeeAvaliacaoCache.comment_id == str(comment_id)).first())
    if l is None:
        l = ShopeeAvaliacaoCache(user_id=user_id, comment_id=str(comment_id))
        db.add(l)
    l.respondida = True
    l.resposta = resposta
    l.respondida_em = datetime.utcnow()
    l.ultimo_erro = None
    db.commit()


def marcar_falha(db, user_id: int, comment_id: str, erro: str) -> int:
    """Registra a falha e conta a tentativa. Depois de MAX_TENTATIVAS, para de tentar
    — em vez de insistir e acumular rejeição no histórico do parceiro."""
    l = (db.query(ShopeeAvaliacaoCache)
         .filter(ShopeeAvaliacaoCache.user_id == user_id,
                 ShopeeAvaliacaoCache.comment_id == str(comment_id)).first())
    if l is None:
        return 0
    l.tentativas = (l.tentativas or 0) + 1
    l.ultimo_erro = str(erro)[:250]
    # se a Shopee disse que já existe resposta, marca como respondida e nunca mais tenta
    if any(t in str(erro).lower() for t in ("already", "duplicate", "replied", "exist")):
        l.respondida = True
        l.respondida_em = datetime.utcnow()
    db.commit()
    return l.tentativas


def estado(db, user_id: int) -> dict:
    base = db.query(ShopeeAvaliacaoCache).filter(ShopeeAvaliacaoCache.user_id == user_id)
    total = base.count()
    resp = base.filter(ShopeeAvaliacaoCache.respondida == True).count()  # noqa: E712
    travadas = base.filter(ShopeeAvaliacaoCache.respondida == False,  # noqa: E712
                           ShopeeAvaliacaoCache.tentativas >= MAX_TENTATIVAS).count()
    ult = (base.filter(ShopeeAvaliacaoCache.respondida == True)  # noqa: E712
           .order_by(ShopeeAvaliacaoCache.respondida_em.desc()).first())
    return {
        "total_no_cache": total, "respondidas": resp,
        "pendentes": pendentes_qtd(db, user_id), "travadas": travadas,
        "ultima_resposta": ult.respondida_em.isoformat() if ult and ult.respondida_em else None,
        "economia": "varre só UNANSWERED — respondidas nunca são relidas",
    }


def semear_respondidas(db, user_id: int, max_paginas: int = 30) -> dict:
    """Carga ÚNICA: traz o histórico de respondidas para o cache, para que a partir
    daí nunca mais precisem ser lidas. Roda uma vez só."""
    chamadas = gravadas = 0
    cursor = ""
    for _ in range(max_paginas):
        try:
            r = shopee.comentarios_brutos(user_id, status="ANSWERED",
                                          cursor=cursor, limite=100)
            chamadas += 1
        except Exception as e:  # noqa: BLE001
            print(f"[avaliacoes] semeadura parou: {e}", flush=True)
            break
        lote = r.get("item_comment_list") or []
        for c in lote:
            _gravar(db, user_id, c)
            gravadas += 1
        db.commit()
        if not r.get("more") or not r.get("next_cursor"):
            break
        cursor = r.get("next_cursor")
    return {"ok": True, "chamadas_api": chamadas, "gravadas": gravadas,
            "aviso": "carga única — a partir de agora só UNANSWERED é lido"}


# ─────────────────────────────────────────────────────────────────────────────
# VARREDURA PROFUNDA — alcança as avaliações ALÉM do teto de ~1.000
#
# A paginação global do get_comment para em torno de 1.000 registros (a própria
# Shopee limita a profundidade do cursor). Com 1.600+ pendentes, as mais antigas
# ficam inalcançáveis por ali. O único caminho é varrer POR PRODUTO (item_id).
#
# Isso custa 1 chamada por produto — caro se repetido a cada ciclo (era o defeito
# antigo). Aqui roda SOB DEMANDA e o resultado fica no cache: cada avaliação
# encontrada nunca mais precisa ser buscada.
# ─────────────────────────────────────────────────────────────────────────────
_PROG_PROFUNDA: dict = {}


def progresso_profunda(user_id: int) -> dict:
    return _PROG_PROFUNDA.get(user_id) or {"em_andamento": False}


def varredura_profunda(db, user_id: int, item_ids: list = None,
                       max_produtos: int = 6000, pausa: float = 0.12) -> dict:
    """Percorre produto a produto atrás das avaliações antigas sem resposta."""
    from . import shopee as _sh
    import time as _t

    ids = [int(i) for i in (item_ids or []) if i]
    if not ids:
        # TODOS os status: avaliação antiga quase sempre está em produto que hoje
        # está DESATIVADO ou ESGOTADO. Varrer só NORMAL deixava essas de fora —
        # era a causa das ~1.000 antigas que nunca apareciam.
        vistos_ids = set()
        for st in ("NORMAL", "UNLIST", "BANNED", "DELETED"):
            try:
                offset = 0
                for _ in range(60):
                    r = _sh._chamar(user_id, "/api/v2/product/get_item_list",
                                    extra={"offset": offset, "page_size": 100,
                                           "item_status": st})
                    resp = r.get("response") or {}
                    lote = resp.get("item") or []
                    novos = [int(x.get("item_id")) for x in lote if x.get("item_id")]
                    for n in novos:
                        if n not in vistos_ids:
                            vistos_ids.add(n)
                            ids.append(n)
                    offset += len(lote)
                    if not resp.get("has_next_page") or not lote:
                        break
            except Exception as e:  # noqa: BLE001
                print(f"[profunda] status {st}: {str(e)[:110]}", flush=True)
        if not ids:
            return {"ok": False, "erro": "não consegui listar nenhum produto da loja"}
        print(f"[profunda] {len(ids)} produtos em todos os status", flush=True)

    ids = ids[:max_produtos]
    _PROG_PROFUNDA[user_id] = {"em_andamento": True, "total": len(ids), "feitos": 0,
                               "encontradas": 0, "chamadas": 0,
                               "inicio": datetime.utcnow().isoformat()}
    achadas = chamadas = 0
    ja_no_cache = {r[0] for r in db.query(ShopeeAvaliacaoCache.comment_id)
                   .filter(ShopeeAvaliacaoCache.user_id == user_id).all()}
    for n, iid in enumerate(ids, 1):
        try:
            # PAGINA dentro do produto: um item campeão pode ter centenas de
            # avaliações antigas — pegar só as 100 primeiras perdia o resto.
            cursor_i, paginas = "", 0
            while paginas < 20:
                r = _sh.comentarios_brutos(user_id, item_id=iid, status="UNANSWERED",
                                           cursor=cursor_i, limite=100)
                chamadas += 1
                paginas += 1
                lote = r.get("item_comment_list") or []
                for c in lote:
                    cid = str(c.get("comment_id") or "")
                    _gravar(db, user_id, c)
                    if cid and cid not in ja_no_cache:
                        achadas += 1
                        ja_no_cache.add(cid)
                if not r.get("more") or not r.get("next_cursor"):
                    break
                cursor_i = r.get("next_cursor")
                _t.sleep(0.08)
            if n % 25 == 0:
                db.commit()
        except Exception as e:  # noqa: BLE001
            print(f"[profunda] item {iid}: {str(e)[:90]}", flush=True)
        _PROG_PROFUNDA[user_id].update({"feitos": n, "encontradas": achadas, "chamadas": chamadas})
        if pausa:
            _t.sleep(pausa)     # respiro entre chamadas — não estressa a API
    db.commit()
    _PROG_PROFUNDA[user_id].update({"em_andamento": False, "fim": datetime.utcnow().isoformat()})
    return {"ok": True, "produtos_varridos": len(ids), "chamadas_api": chamadas,
            "avaliacoes_novas": achadas, "pendentes_agora": pendentes_qtd(db, user_id),
            "aviso": "carga única — o que foi encontrado fica no cache e não é buscado de novo"}


def diagnostico_cobertura(db, user_id: int) -> dict:
    """Mostra ONDE estão as pendentes: por produto, por idade e o que ainda falta
    varrer. É como se confirma que a varredura profunda alcançou tudo."""
    from collections import Counter
    from datetime import datetime as _dt
    base = db.query(ShopeeAvaliacaoCache).filter(ShopeeAvaliacaoCache.user_id == user_id)
    pend = base.filter(ShopeeAvaliacaoCache.respondida == False).all()  # noqa: E712
    agora = int(_dt.utcnow().timestamp())

    faixas = {"até 7 dias": 0, "8 a 30 dias": 0, "31 a 90 dias": 0,
              "91 a 180 dias": 0, "mais de 180 dias": 0, "sem data": 0}
    for a in pend:
        ct = a.create_time or 0
        if not ct:
            faixas["sem data"] += 1
            continue
        dias = (agora - ct) / 86400
        if dias <= 7:
            faixas["até 7 dias"] += 1
        elif dias <= 30:
            faixas["8 a 30 dias"] += 1
        elif dias <= 90:
            faixas["31 a 90 dias"] += 1
        elif dias <= 180:
            faixas["91 a 180 dias"] += 1
        else:
            faixas["mais de 180 dias"] += 1

    por_item = Counter(a.item_id for a in pend if a.item_id)
    mais_antiga = min((a.create_time for a in pend if a.create_time), default=None)
    mais_nova = max((a.create_time for a in pend if a.create_time), default=None)
    return {
        "pendentes": len(pend),
        "respondidas": base.filter(ShopeeAvaliacaoCache.respondida == True).count(),  # noqa: E712
        "total_no_cache": base.count(),
        "travadas_por_falha": base.filter(
            ShopeeAvaliacaoCache.respondida == False,  # noqa: E712
            ShopeeAvaliacaoCache.tentativas >= MAX_TENTATIVAS).count(),
        "por_idade": faixas,
        "produtos_com_pendencia": len(por_item),
        "top_produtos": [{"item_id": i, "pendentes": n} for i, n in por_item.most_common(10)],
        "mais_antiga": (_dt.utcfromtimestamp(mais_antiga).isoformat() if mais_antiga else None),
        "mais_nova": (_dt.utcfromtimestamp(mais_nova).isoformat() if mais_nova else None),
        "leitura": (f"{len(pend)} pendentes em {len(por_item)} produtos · "
                    f"{faixas['mais de 180 dias']} com mais de 180 dias"),
    }


def diagnostico_api(user_id: int, item_id=None) -> dict:
    """MEDE o que a API realmente devolve, em vez de deduzir.

    Responde três perguntas que decidem tudo:
      1. Quantos produtos a listagem devolve, por status?
      2. Paginando UM produto até o fim, quantas avaliações vêm e de que datas?
      3. Existe um limite de idade? (a avaliação mais antiga alcançável)
    """
    from datetime import datetime as _dt
    from . import shopee as _sh
    out = {"passos": [], "produtos_por_status": {}, "amostra_item": {}}

    # 1) produtos por status
    total_ids = set()
    for st in ("NORMAL", "UNLIST", "BANNED", "DELETED"):
        n, offset, erro = 0, 0, None
        try:
            for _ in range(60):
                r = _sh._chamar(user_id, "/api/v2/product/get_item_list",
                                extra={"offset": offset, "page_size": 100, "item_status": st})
                resp = r.get("response") or {}
                lote = resp.get("item") or []
                n += len(lote)
                for x in lote:
                    if x.get("item_id"):
                        total_ids.add(int(x["item_id"]))
                offset += len(lote)
                if not resp.get("has_next_page") or not lote:
                    break
        except Exception as e:  # noqa: BLE001
            erro = str(e)[:180]
        out["produtos_por_status"][st] = {"produtos": n, "erro": erro}
    out["produtos_unicos"] = len(total_ids)
    out["passos"].append(f"1) produtos: {len(total_ids)} únicos em 4 status")

    # 2) um produto, paginado até o fim — mostra datas e onde para
    alvo = int(item_id) if item_id else (sorted(total_ids)[0] if total_ids else None)
    if alvo:
        datas, paginas, cursor, erro = [], 0, "", None
        try:
            while paginas < 30:
                r = _sh.comentarios_brutos(user_id, item_id=alvo, status="UNANSWERED",
                                           cursor=cursor, limite=100)
                paginas += 1
                lote = r.get("item_comment_list") or []
                datas += [c.get("create_time") for c in lote if c.get("create_time")]
                if not r.get("more") or not r.get("next_cursor"):
                    break
                cursor = r.get("next_cursor")
        except Exception as e:  # noqa: BLE001
            erro = str(e)[:200]
        if datas:
            out["amostra_item"] = {
                "item_id": alvo, "paginas_lidas": paginas, "avaliacoes": len(datas),
                "mais_nova": _dt.utcfromtimestamp(max(datas)).isoformat(),
                "mais_antiga": _dt.utcfromtimestamp(min(datas)).isoformat(),
                "idade_maxima_dias": round((_dt.utcnow().timestamp() - min(datas)) / 86400, 1),
                "erro": erro,
            }
        else:
            out["amostra_item"] = {"item_id": alvo, "paginas_lidas": paginas,
                                   "avaliacoes": 0, "erro": erro,
                                   "obs": "produto sem avaliação pendente"}
    out["passos"].append(f"2) amostra do item {alvo}: {out['amostra_item'].get('avaliacoes', 0)} pendentes")

    # 3) varredura GLOBAL até onde o cursor deixa — revela o teto de idade
    datas_g, paginas_g, cursor_g, erro_g = [], 0, "", None
    try:
        while paginas_g < 40:
            r = _sh.comentarios_brutos(user_id, status="UNANSWERED", cursor=cursor_g, limite=100)
            paginas_g += 1
            lote = r.get("item_comment_list") or []
            datas_g += [c.get("create_time") for c in lote if c.get("create_time")]
            if not r.get("more") or not r.get("next_cursor"):
                break
            cursor_g = r.get("next_cursor")
    except Exception as e:  # noqa: BLE001
        erro_g = str(e)[:200]
    if datas_g:
        idade = round((_dt.utcnow().timestamp() - min(datas_g)) / 86400, 1)
        out["global"] = {
            "paginas_lidas": paginas_g, "avaliacoes": len(datas_g),
            "mais_antiga": _dt.utcfromtimestamp(min(datas_g)).isoformat(),
            "idade_maxima_dias": idade, "parou_por": ("fim do cursor" if paginas_g < 40 else "teto de 40 páginas"),
            "erro": erro_g,
        }
        out["passos"].append(f"3) varredura global: {len(datas_g)} pendentes, a mais antiga com {idade} dias")
        out["veredito"] = (
            f"a API devolve até {idade} dias de idade. "
            + ("Se o Seller Center mostra avaliações MAIS ANTIGAS que isso, a API tem limite de janela "
               "e essas só são respondíveis pelo painel da Shopee."
               if idade < 400 else
               "A API alcança avaliações antigas — o problema está na nossa varredura."))
    else:
        out["global"] = {"avaliacoes": 0, "erro": erro_g}
        out["veredito"] = "a varredura global não devolveu nada — ver o erro acima"
    return out


def sla_resposta(db, user_id: int) -> dict:
    """Velocidade de resposta a partir do cache. Usa o horário REAL da resposta
    (comment_reply.create_time no payload cru) menos o create_time da avaliação.
    Só entram avaliações com os dois tempos — nada de estimativa. `amostra` diz
    quantas serviram; se for pequena, a tela mostra 'acumulando'."""
    import time as _t
    linhas = (db.query(ShopeeAvaliacaoCache)
              .filter(ShopeeAvaliacaoCache.user_id == user_id).all())
    deltas = []          # segundos entre avaliação e resposta
    pend_fora = 0        # pendentes que já passaram de 48h
    agora = int(_t.time())
    for l in linhas:
        ct = l.create_time or 0
        if not l.respondida:
            if ct and (agora - ct) > 48 * 3600:
                pend_fora += 1
            continue
        pl = l.payload if isinstance(l.payload, dict) else {}
        rt = ((pl.get("comment_reply") or {}).get("create_time")) or 0
        if ct and rt and rt >= ct:
            deltas.append(rt - ct)
    deltas.sort()
    n = len(deltas)

    def _pct(p):
        if not n:
            return None
        i = min(n - 1, int(round((p / 100) * (n - 1))))
        return deltas[i]

    def _fmt(s):
        if s is None:
            return None
        if s < 3600:
            return f"{max(1, round(s / 60))}min"
        if s < 86400:
            h = s / 3600
            return (f"{h:.0f}h" if abs(h - round(h)) < 0.05 else f"{h:.1f}h")
        return f"{round(s / 86400)}d"

    faixas = {"<1h": 0, "1-6h": 0, "6-24h": 0, ">24h": 0}
    for s in deltas:
        if s < 3600:
            faixas["<1h"] += 1
        elif s < 6 * 3600:
            faixas["1-6h"] += 1
        elif s < 24 * 3600:
            faixas["6-24h"] += 1
        else:
            faixas[">24h"] += 1
    pct = {k: (round(v / n * 100) if n else 0) for k, v in faixas.items()}
    dentro = sum(1 for s in deltas if s <= 48 * 3600)
    return {"amostra": n,
            "mediana_seg": _pct(50), "p90_seg": _pct(90),
            "mediana": _fmt(_pct(50)), "p90": _fmt(_pct(90)),
            "faixas_pct": pct, "faixas_qtd": faixas,
            "pct_no_prazo": (round(dentro / n * 100) if n else None),
            "pendentes_fora_prazo": pend_fora}
