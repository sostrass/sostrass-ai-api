"""Cache de pedidos Shopee — o painel passa a ler do BANCO, não da API.

PROBLEMA QUE ISTO RESOLVE
Antes, cada abertura do painel disparava até 12 páginas de 50 pedidos na API da
Shopee, mais o desmascaramento em lotes. Abrir o painel 20 vezes num dia
significava centenas de chamadas — um padrão de acesso agressivo e desnecessário.

COMO PASSA A FUNCIONAR
  · O painel lê de `shopee_pedido_cache` — ZERO chamadas à API.
  · A sincronização roda sob demanda (botão) ou em fundo, e busca só o DELTA:
    pedidos com `update_time` maior que a última marca d'água.
  · Um pedido específico pode ser atualizado sozinho (`atualizar_um`), que é o que
    acontece quando você age sobre ele no painel.

`shopee.py` é FROZEN — este módulo só o consome.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import shopee
from .models import ShopeePedidoCache, SyncEstado


def _estado(db, user_id: int) -> SyncEstado:
    e = (db.query(SyncEstado)
         .filter(SyncEstado.user_id == user_id, SyncEstado.canal == "shopee").first())
    if e is None:
        e = SyncEstado(user_id=user_id, canal="shopee", pedidos_cache=0)
        db.add(e)
        db.commit()
        db.refresh(e)
    return e


def _gravar(db, user_id: int, p: dict) -> bool:
    """Grava/atualiza um pedido no cache. Devolve True se mudou algo."""
    sn = str(p.get("order_sn") or p.get("id") or "").strip()
    if not sn:
        return False
    linha = (db.query(ShopeePedidoCache)
             .filter(ShopeePedidoCache.user_id == user_id,
                     ShopeePedidoCache.order_sn == sn).first())
    novo_ut = p.get("update_time") or p.get("atualizado") or 0
    if linha is not None and novo_ut and (linha.update_time or 0) >= novo_ut:
        return False   # já temos a versão mais nova — não regrava
    if linha is None:
        linha = ShopeePedidoCache(user_id=user_id, order_sn=sn)
        db.add(linha)
    linha.status = p.get("status") or p.get("order_status")
    linha.update_time = novo_ut or linha.update_time
    linha.create_time = p.get("create_time") or p.get("criado") or linha.create_time
    linha.ship_by = p.get("ship_by") or linha.ship_by
    linha.total = p.get("total") or p.get("valor") or p.get("receita") or linha.total
    linha.comprador = p.get("comprador") or p.get("buyer_username") or linha.comprador
    linha.cliente = p.get("cliente") or linha.cliente
    linha.cidade = p.get("cidade") or linha.cidade
    linha.uf = p.get("uf") or p.get("estado") or linha.uf
    linha.rastreio = p.get("rastreio") or p.get("tracking_number") or linha.rastreio
    linha.nf_numero = p.get("nf_numero") or linha.nf_numero
    linha.payload = p
    linha.sincronizado_em = datetime.utcnow()
    return True


def sincronizar(db, user_id: int, dias: int = 15, completo: bool = False,
                max_paginas: int = 12) -> dict:
    """Busca na API e atualiza o cache. Por padrão só o DELTA (mais rápido e leve).

    completo=True refaz tudo (usar só na primeira carga ou quando pedir explicitamente).
    """
    est = _estado(db, user_id)
    chamadas = 0
    novos = mudados = 0
    vistos = set()
    marca = est.ultimo_update_time or 0

    for pg in range(1, max_paginas + 1):
        try:
            r = shopee.pedidos_painel(user_id, status="TODOS", dias=dias,
                                      page=pg, page_size=50)
            chamadas += 1
        except Exception as e:  # noqa: BLE001
            print(f"[shopee_cache] página {pg} falhou: {e}", flush=True)
            break
        lote = (r or {}).get("pedidos") or []
        if not lote:
            break
        parou_no_delta = False
        for p in lote:
            sn = str(p.get("order_sn") or p.get("id") or "")
            if not sn or sn in vistos:
                continue
            vistos.add(sn)
            ut = p.get("update_time") or 0
            if not completo and marca and ut and ut <= marca:
                parou_no_delta = True     # daqui para trás já está no cache
                continue
            existia = (db.query(ShopeePedidoCache)
                       .filter(ShopeePedidoCache.user_id == user_id,
                               ShopeePedidoCache.order_sn == sn).count() > 0)
            if _gravar(db, user_id, p):
                if existia:
                    mudados += 1
                else:
                    novos += 1
            if ut and ut > (est.ultimo_update_time or 0):
                est.ultimo_update_time = ut
        db.commit()
        # no modo delta, se a página inteira já era conhecida, para de paginar
        if not completo and parou_no_delta and novos == 0 and mudados == 0:
            break
        if len(lote) < 50:
            break

    est.ultimo_sync = datetime.utcnow()
    est.chamadas_ultimo_sync = chamadas
    est.pedidos_cache = (db.query(ShopeePedidoCache)
                         .filter(ShopeePedidoCache.user_id == user_id).count())
    db.commit()
    return {"ok": True, "novos": novos, "mudados": mudados, "chamadas_api": chamadas,
            "pedidos_no_cache": est.pedidos_cache, "modo": "completo" if completo else "delta",
            "sincronizado_em": est.ultimo_sync.isoformat()}


def atualizar_um(db, user_id: int, order_sn: str) -> dict:
    """Atualiza UM pedido — é o que roda quando você age sobre ele no painel.
    Custa 1 chamada, não 12 páginas."""
    try:
        r = shopee.pedidos_painel(user_id, status="TODOS", dias=60, page=1, page_size=50,
                                  busca=str(order_sn), busca_tipo="pedido")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": str(e)[:200]}
    achou = None
    for p in ((r or {}).get("pedidos") or []):
        if str(p.get("order_sn") or p.get("id")) == str(order_sn):
            achou = p
            break
    if achou is None:
        return {"ok": False, "erro": "pedido não encontrado na API"}
    _gravar(db, user_id, achou)
    db.commit()
    return {"ok": True, "order_sn": order_sn, "chamadas_api": 1}


def listar(db, user_id: int, status: str = "TODOS", dias: int = 15,
           page: int = 1, page_size: int = 50, busca: str = "") -> dict:
    """Lê do BANCO. Nenhuma chamada à API. É isto que o painel usa."""
    corte = int((datetime.utcnow() - timedelta(days=dias)).timestamp())
    q = db.query(ShopeePedidoCache).filter(ShopeePedidoCache.user_id == user_id)
    if dias:
        q = q.filter((ShopeePedidoCache.create_time.is_(None))
                     | (ShopeePedidoCache.create_time >= corte))
    grupos = {
        "NAO_PAGO": ["UNPAID"],
        "A_ENVIAR": ["READY_TO_SHIP", "PROCESSED", "RETRY_SHIP"],
        "ENVIADO": ["SHIPPED", "TO_CONFIRM_RECEIVE"],
        "CONCLUIDO": ["COMPLETED"],
        "RETORNOS": ["CANCELLED", "IN_CANCEL", "TO_RETURN"],
    }
    if status and status != "TODOS" and status in grupos:
        q = q.filter(ShopeePedidoCache.status.in_(grupos[status]))
    if busca:
        t = f"%{busca.strip()}%"
        q = q.filter((ShopeePedidoCache.order_sn.ilike(t))
                     | (ShopeePedidoCache.comprador.ilike(t))
                     | (ShopeePedidoCache.cliente.ilike(t))
                     | (ShopeePedidoCache.rastreio.ilike(t)))
    total = q.count()
    linhas = (q.order_by(ShopeePedidoCache.create_time.desc().nullslast()
                         if hasattr(ShopeePedidoCache.create_time.desc(), "nullslast")
                         else ShopeePedidoCache.create_time.desc())
              .offset((page - 1) * page_size).limit(page_size).all())
    est = _estado(db, user_id)
    return {
        "pedidos": [l.payload or {} for l in linhas],
        "paging": {"page": page, "page_size": page_size, "total": total,
                   "paginas": max(1, (total + page_size - 1) // page_size)},
        "cache": {
            "do_banco": True, "chamadas_api": 0,
            "ultimo_sync": est.ultimo_sync.isoformat() if est.ultimo_sync else None,
            "pedidos_no_cache": est.pedidos_cache,
            "minutos_desde_sync": (int((datetime.utcnow() - est.ultimo_sync).total_seconds() / 60)
                                   if est.ultimo_sync else None),
        },
    }


def estado(db, user_id: int) -> dict:
    est = _estado(db, user_id)
    n = db.query(ShopeePedidoCache).filter(ShopeePedidoCache.user_id == user_id).count()
    return {
        "pedidos_no_cache": n,
        "ultimo_sync": est.ultimo_sync.isoformat() if est.ultimo_sync else None,
        "minutos_desde_sync": (int((datetime.utcnow() - est.ultimo_sync).total_seconds() / 60)
                               if est.ultimo_sync else None),
        "chamadas_ultimo_sync": est.chamadas_ultimo_sync or 0,
        "precisa_sync": n == 0 or est.ultimo_sync is None,
    }
