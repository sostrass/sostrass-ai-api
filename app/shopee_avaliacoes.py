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


def varrer_pendentes(db, user_id: int, max_paginas: int = 5) -> dict:
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
