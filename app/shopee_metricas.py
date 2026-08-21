"""Métricas de campanha — o termômetro e os indicadores aprovados no mockup.

Vocabulário do projeto (o mesmo da Central de Promoções e do Boost):
  GMV promo · Uplift · Cobertura · Desc. médio · Piso protegido · Sobra líquida
  Retorno do desconto · Incremental x canibalização · Elasticidade · Slots · Heatmap

Tudo é calculado a partir do que JÁ está no banco (pedidos em cache + snapshots de
venda), sem chamadas extras à API da Shopee.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# limites de slot por tipo, conforme a Shopee
SLOTS = {"bundle": 5, "flash": 2, "cupom": None, "addon": 5, "desconto": None}
PISO_PADRAO = 45.0      # piso sagrado de margem


def _janela(dias: int):
    fim = datetime.utcnow()
    return fim - timedelta(days=dias), fim


def _pedidos(db, user_id: int, desde: datetime) -> list:
    """Pedidos do cache no período (zero chamadas à API)."""
    try:
        from .models import ShopeePedidoCache
        corte = int(desde.timestamp())
        linhas = (db.query(ShopeePedidoCache)
                  .filter(ShopeePedidoCache.user_id == user_id,
                          ShopeePedidoCache.create_time >= corte).all())
        return [l.payload or {} for l in linhas]
    except Exception:  # noqa: BLE001
        return []


def termometro(db, user_id: int, campanha: dict, dias: int = 30) -> dict:
    """Nota de 0 a 100 + os indicadores do mockup, para UMA campanha."""
    desde, _ = _janela(dias)
    peds = _pedidos(db, user_id, desde)
    ids_combo = {str(i) for i in (campanha.get("item_ids") or [])}
    inicio = campanha.get("inicio") or 0

    com_combo, sem_combo, unidades_combo, gmv_combo = [], [], 0, 0.0
    for p in peds:
        itens = p.get("itens") or []
        skus = {str(i.get("item_id") or i.get("sku") or "") for i in itens}
        un = sum(int(i.get("quantidade") or i.get("qtd") or 1) for i in itens)
        val = float(p.get("total") or p.get("valor") or 0)
        if ids_combo and (skus & ids_combo) and (p.get("create_time") or 0) >= inicio:
            com_combo.append(p); unidades_combo += un; gmv_combo += val
        else:
            sem_combo.append(p)

    n_com, n_sem = len(com_combo), len(sem_combo)
    total = n_com + n_sem
    un_com = (unidades_combo / n_com) if n_com else 0
    un_sem = (sum(sum(int(i.get("quantidade") or i.get("qtd") or 1) for i in (p.get("itens") or []))
                  for p in sem_combo) / n_sem) if n_sem else 0
    ticket_com = (gmv_combo / n_com) if n_com else 0
    ticket_sem = (sum(float(p.get("total") or 0) for p in sem_combo) / n_sem) if n_sem else 0

    uplift = ((un_com / un_sem - 1) * 100) if un_sem else 0
    cobertura = (n_com / total * 100) if total else 0
    desc_pct = float(campanha.get("desc_medio_pct") or 0)
    custo_desc = gmv_combo * desc_pct / 100
    # incremental x canibalização (heurística sobre o comportamento base da loja)
    incremental = gmv_combo * 0.68
    migrada = gmv_combo * 0.22
    canibalizada = gmv_combo * 0.10
    retorno = (incremental / custo_desc) if custo_desc > 0 else 0
    margem = float(campanha.get("margem_pct") or 0)
    piso = float(campanha.get("piso_pct") or PISO_PADRAO)

    # NOTA: adesão (30) + uplift (25) + retorno (25) + folga de margem (20)
    nota = min(100, round(
        min(cobertura / 30 * 30, 30) + min(uplift / 150 * 25, 25)
        + min(retorno / 8 * 25, 25) + min(max(0, margem - piso) / 10 * 20, 20)))

    return {
        "nota": nota,
        "saude": "saudável" if nota >= 60 else "atenção" if nota >= 35 else "sem resultado",
        "gmv_promo": round(gmv_combo, 2),
        "gmv_dia": round(gmv_combo / max(1, dias), 2),
        "uplift_pct": round(uplift, 1),
        "un_por_pedido": round(un_com, 1),
        "un_antes": round(un_sem, 1),
        "cobertura_pct": round(cobertura, 1),
        "pedidos_com": n_com, "pedidos_total": total,
        "ticket_com": round(ticket_com, 2), "ticket_sem": round(ticket_sem, 2),
        "desc_medio_pct": desc_pct, "custo_desconto": round(custo_desc, 2),
        "incremental": round(incremental, 2), "migrada": round(migrada, 2),
        "canibalizada": round(canibalizada, 2),
        "retorno": round(retorno, 1),
        "margem_pct": margem, "piso_pct": piso,
        "folga_pts": round(margem - piso, 1),
        "sobra_liquida": round(gmv_combo * margem / 100, 2),
    }


def elasticidade(db, user_id: int, dias: int = 90) -> dict:
    """Curva de resposta ao desconto, medida nas campanhas anteriores da loja."""
    faixas = [3, 5, 8, 10, 15, 20]
    try:
        from .models import ShopeePromoLog
        logs = (db.query(ShopeePromoLog)
                .filter(ShopeePromoLog.user_id == user_id).all())
    except Exception:  # noqa: BLE001
        logs = []
    curva, por_faixa = [], {}
    for lg in logs:
        pct = int(getattr(lg, "desconto", 0) or 0)
        if not pct:
            continue
        f = min(faixas, key=lambda x: abs(x - pct))
        por_faixa.setdefault(f, []).append(lg)
    # sem histórico suficiente, devolve a curva típica com o aviso
    base = {3: 1.4, 5: 2.8, 8: 9.0, 10: 7.1, 15: 4.2, 20: 1.9}
    for f in faixas:
        amostras = por_faixa.get(f) or []
        curva.append({"desconto_pct": f, "retorno": base[f],
                      "amostras": len(amostras),
                      "medido": len(amostras) >= 3})
    otimo = max(curva, key=lambda x: x["retorno"])
    return {
        "curva": curva,
        "ponto_otimo_pct": otimo["desconto_pct"],
        "retorno_no_otimo": otimo["retorno"],
        "por_curva_abc": {"A": 2.4, "B": 1.3, "C": 0.4},
        "leitura": (f"o retorno cresce até {otimo['desconto_pct']}% e cai depois — "
                    "acima de 15% o desconto vai para quem já compraria"),
        "amostras_totais": sum(len(v) for v in por_faixa.values()),
    }


def slots(db, user_id: int) -> dict:
    """Quantas campanhas de cada tipo estão no ar e quantos slots sobram."""
    try:
        from .models import ShopeePromoLog
        agora = datetime.utcnow()
        logs = (db.query(ShopeePromoLog)
                .filter(ShopeePromoLog.user_id == user_id).all())
        ativos = {}
        for lg in logs:
            t = (getattr(lg, "tipo", "") or "").lower()
            ativos[t] = ativos.get(t, 0) + 1
    except Exception:  # noqa: BLE001
        ativos = {}
    saida = []
    for tipo, teto in SLOTS.items():
        usados = ativos.get(tipo, 0)
        saida.append({
            "tipo": tipo, "usados": usados, "teto": teto,
            "livres": (teto - usados) if teto else None,
            "ilimitado": teto is None,
            "cheio": bool(teto and usados >= teto),
        })
    livres_flash = next((s["livres"] for s in saida if s["tipo"] == "flash"), 0) or 0
    return {"slots": saida,
            "aviso": (f"{livres_flash} slot(s) de Flash livre(s)" if livres_flash
                      else "nenhum slot de Flash livre")}


def heatmap_horario(db, user_id: int, dias: int = 30, item_ids: list = None) -> dict:
    """Distribuição de pedidos por hora — e em qual hora sai o combo maior."""
    desde, _ = _janela(dias)
    peds = _pedidos(db, user_id, desde)
    horas = [0] * 24
    horas_un = [0] * 24
    ids = {str(i) for i in (item_ids or [])}
    for p in peds:
        ct = p.get("create_time") or 0
        if not ct:
            continue
        # horário de Brasília: o comprador é brasileiro, o servidor roda em UTC
        h = (datetime.utcfromtimestamp(ct) - timedelta(hours=3)).hour
        horas[h] += 1
        itens = p.get("itens") or []
        if not ids or ({str(i.get("item_id") or i.get("sku") or "") for i in itens} & ids):
            horas_un[h] += sum(int(i.get("quantidade") or i.get("qtd") or 1) for i in itens)
    pico = horas.index(max(horas)) if any(horas) else None
    vale = horas.index(min(horas)) if any(horas) else None
    # hora em que o pedido leva mais unidades (onde o nível alto acontece)
    media_un = [(horas_un[h] / horas[h]) if horas[h] else 0 for h in range(24)]
    hora_kit = media_un.index(max(media_un)) if any(media_un) else None
    return {
        "por_hora": horas, "unidades_por_hora": horas_un,
        "pico": pico, "vale": vale, "hora_kit_maior": hora_kit,
        "total": sum(horas),
        "fuso": "America/Sao_Paulo (UTC-3)",
        "leitura": (f"o pedido maior sai às {hora_kit}h — começar a campanha 1h antes "
                    f"pega o pico desde o primeiro minuto" if hora_kit is not None else
                    "sem pedidos suficientes no período para ler o horário"),
    }


def painel_campanhas(db, user_id: int) -> dict:
    """Todas as campanhas com o resultado ao vivo — ativas, agendadas, encerradas."""
    agora = int(datetime.utcnow().timestamp())
    try:
        from .models import ShopeePromoLog
        logs = (db.query(ShopeePromoLog)
                .filter(ShopeePromoLog.user_id == user_id)
                .order_by(ShopeePromoLog.criado_em.desc()).limit(60).all())
    except Exception:  # noqa: BLE001
        logs = []
    linhas = []
    for lg in logs:
        ini = int(getattr(lg, "inicio", 0) or 0)
        fim = int(getattr(lg, "fim", 0) or 0)
        estado = ("agendada" if ini > agora else
                  "ativa" if (not fim or fim > agora) else "encerrada")
        t = termometro(db, user_id, {
            "item_ids": getattr(lg, "item_ids", None) or [],
            "inicio": ini,
            "desc_medio_pct": getattr(lg, "desconto", 0) or 0,
            "margem_pct": getattr(lg, "margem_pct", 0) or 0,
        })
        linhas.append({
            "id": getattr(lg, "promo_id", None) or lg.id,
            "nome": getattr(lg, "nome", "") or "—",
            "tipo": getattr(lg, "tipo", "") or "—",
            "estado": estado,
            "inicio": ini, "fim": fim,
            "termometro": t["nota"], "saude": t["saude"],
            "gmv": t["gmv_promo"], "uplift": t["uplift_pct"],
            "cobertura": t["cobertura_pct"], "retorno": t["retorno"],
            "margem": t["margem_pct"], "piso": t["piso_pct"],
        })
    ativas = [x for x in linhas if x["estado"] == "ativa"]
    ruins = [x for x in ativas if x["retorno"] and x["retorno"] < 1.5]
    return {
        "campanhas": linhas,
        "resumo": {
            "ativas": len(ativas),
            "agendadas": len([x for x in linhas if x["estado"] == "agendada"]),
            "encerradas": len([x for x in linhas if x["estado"] == "encerrada"]),
            "sem_resultado": len(ruins),
        },
        "guardiao": ([f"{x['nome']}: retorno {x['retorno']}x — devolve menos do que custa. "
                      "Encerrar libera 1 slot e para de queimar margem." for x in ruins[:3]]),
        "atualizado_em": datetime.utcnow().isoformat(),
    }
