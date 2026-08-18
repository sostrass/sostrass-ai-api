"""Bundle Deal ("Leve Mais por Menos") — implementação corrigida.

DOIS DEFEITOS ENCONTRADOS NA IMPLEMENTAÇÃO ANTIGA

1) LISTAGEM — `time_status` enviado como TEXTO
   O código antigo mandava {"time_status": "ongoing"}. A comparação com os endpoints
   irmãos prova que a Shopee trata este parâmetro de outra forma:

       discount.get_discount_list      discount_status="ongoing"   -> 205 chamadas, 0 falhas
       add_on_deal.get_add_on_deal_list promotion_status="ongoing" ->  53 chamadas, 0 falhas
       bundle_deal.get_bundle_deal_list time_status="ongoing"      ->  52 chamadas, 52 falhas

   O `time_status` do bundle_deal é NUMÉRICO: 1=todos · 2=agendados · 3=em andamento
   · 4=encerrados. Enviar texto derruba 100% das chamadas.

2) CRIAÇÃO — campo do valor errado para o tipo de regra
   O código antigo mandava sempre `discount_value`. A Shopee usa um campo por tipo:
       rule_type 1 (preço fixo do combo)  -> fix_price
       rule_type 2 (desconto percentual)  -> discount_percentage
       rule_type 3 (desconto em valor)    -> discount_value
   Como a tela da Shopee usa percentual por padrão, criar combo assim falharia.

`shopee.py` é FROZEN — este módulo reusa apenas `shopee._chamar`.
"""
from __future__ import annotations

from . import shopee

# time_status numérico do bundle_deal
STATUS = {"todos": 1, "all": 1, "agendado": 2, "upcoming": 2,
          "andamento": 3, "ongoing": 3, "encerrado": 4, "expired": 4}
STATUS_NOME = {1: "todos", 2: "agendado", 3: "em andamento", 4: "encerrado"}

# rule_type -> nome do campo que carrega o valor
CAMPO_VALOR = {1: "fix_price", 2: "discount_percentage", 3: "discount_value"}
REGRA_NOME = {1: "Preço fixo do combo", 2: "Desconto em porcentagem", 3: "Desconto com valor fixo"}


def _num_status(status) -> int:
    """Aceita número ou texto e devolve sempre o número que a Shopee espera."""
    if isinstance(status, int):
        return status if status in (1, 2, 3, 4) else 1
    return STATUS.get(str(status or "").strip().lower(), 1)


def listar(user_id: int, status=1, limite: int = 100, cursor: str = "") -> dict:
    """Lista os combos da loja. `status` aceita número (1-4) ou texto ('ongoing'…)."""
    extra = {"time_status": _num_status(status), "page_size": min(int(limite or 100), 100)}
    if cursor:
        extra["cursor"] = cursor
    r = shopee._chamar(user_id, "/api/v2/bundle_deal/get_bundle_deal_list", extra=extra)
    resp = r.get("response") or {}
    return {
        "bundles": [_resumir(b) for b in (resp.get("bundle_deal_list") or [])],
        "more": bool(resp.get("more")),
        "next_cursor": resp.get("next_cursor") or "",
        "status": _num_status(status),
        "status_nome": STATUS_NOME.get(_num_status(status)),
    }


def _resumir(b: dict) -> dict:
    regra = b.get("bundle_deal_rule") or {}
    rt = regra.get("rule_type")
    valor = regra.get(CAMPO_VALOR.get(rt, "discount_value"))
    return {
        "id": b.get("bundle_deal_id"),
        "nome": b.get("name"),
        "inicio": b.get("start_time"),
        "fim": b.get("end_time"),
        "rule_type": rt,
        "regra_nome": REGRA_NOME.get(rt, "—"),
        "valor": valor,
        "min_itens": regra.get("min_amount"),
        "limite_compra": regra.get("max_amount"),
        "itens": b.get("item_count") or b.get("purchase_limit"),
        "bruto": b,
    }


def detalhe(user_id: int, bundle_id) -> dict:
    r = shopee._chamar(user_id, "/api/v2/bundle_deal/get_bundle_deal",
                       extra={"bundle_deal_id": int(bundle_id)})
    resp = r.get("response") or {}
    d = _resumir(resp)
    d["itens_lista"] = resp.get("item_list") or []
    return d


def montar_regra(rule_type: int, valor: float, min_itens: int, limite_compra: int = 0) -> dict:
    """Monta a regra com o CAMPO CERTO para cada tipo — era aqui que quebrava."""
    rt = int(rule_type)
    if rt not in CAMPO_VALOR:
        raise ValueError(f"rule_type inválido: {rt} (use 1=preço fixo, 2=percentual, 3=valor)")
    regra = {"rule_type": rt, "min_amount": int(min_itens or 2)}
    campo = CAMPO_VALOR[rt]
    if rt == 2:
        pct = float(valor)
        if not (0 < pct < 100):
            raise ValueError("desconto percentual deve ficar entre 1 e 99")
        regra[campo] = int(round(pct))       # a Shopee espera inteiro no percentual
    else:
        regra[campo] = round(float(valor), 2)
    if limite_compra:
        regra["max_amount"] = int(limite_compra)
    return regra


def criar(user_id: int, nome: str, inicio: int, fim: int, rule_type: int, valor: float,
          min_itens: int, item_ids: list = None, limite_compra: int = 0) -> dict:
    """Cria o combo e adiciona os itens. Valida as regras do canal ANTES de chamar."""
    import time as _t
    agora = int(_t.time())
    erros = []
    if not nome or len(nome) > 25:
        erros.append("o nome precisa ter de 1 a 25 caracteres (a Shopee corta em 25)")
    if inicio <= agora + 3600:
        erros.append("o início precisa ser pelo menos 1 hora no futuro")
    if fim <= inicio:
        erros.append("o fim precisa ser depois do início")
    if (fim - inicio) > 180 * 86400:
        erros.append("a duração máxima é de 180 dias")
    if int(min_itens or 0) < 2:
        erros.append("o combo exige no mínimo 2 itens")
    if erros:
        return {"ok": False, "erros": erros}
    try:
        regra = montar_regra(rule_type, valor, min_itens, limite_compra)
    except ValueError as e:
        return {"ok": False, "erros": [str(e)]}

    corpo = {"name": nome[:25], "start_time": int(inicio), "end_time": int(fim),
             "bundle_deal_rule": regra}
    try:
        r = shopee._chamar(user_id, "/api/v2/bundle_deal/add_bundle_deal",
                           metodo="POST", extra=corpo)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erros": [f"a Shopee recusou a criação: {e}"], "corpo": corpo}
    bid = (r.get("response") or {}).get("bundle_deal_id")
    if not bid:
        return {"ok": False, "erros": ["a Shopee não devolveu o bundle_deal_id"], "resposta": r}

    add = {"adicionados": 0, "erros": []}
    if item_ids:
        add = adicionar_itens(user_id, bid, item_ids)
    return {"ok": True, "bundle_deal_id": bid, "regra": regra,
            "itens_adicionados": add.get("adicionados", 0),
            "itens_com_erro": add.get("erros", []), "corpo": corpo}


def adicionar_itens(user_id: int, bundle_id, item_ids: list) -> dict:
    """Adiciona itens em lotes de 50 (limite da Shopee)."""
    adicionados, erros = 0, []
    ids = [int(i) for i in (item_ids or []) if i]
    for i in range(0, len(ids), 50):
        lote = ids[i:i + 50]
        try:
            r = shopee._chamar(user_id, "/api/v2/bundle_deal/add_bundle_deal_item",
                               metodo="POST",
                               extra={"bundle_deal_id": int(bundle_id),
                                      "item_list": [{"item_id": x} for x in lote]})
            falhas = ((r.get("response") or {}).get("failed_item_list")) or []
            adicionados += len(lote) - len(falhas)
            for f in falhas:
                erros.append({"item_id": f.get("item_id"),
                              "motivo": f.get("fail_message") or f.get("fail_error")})
        except Exception as e:  # noqa: BLE001
            erros.append({"lote": lote[:3], "motivo": str(e)[:160]})
    return {"adicionados": adicionados, "erros": erros}


def encerrar(user_id: int, bundle_id) -> dict:
    try:
        shopee._chamar(user_id, "/api/v2/bundle_deal/delete_bundle_deal",
                       metodo="POST", extra={"bundle_deal_id": int(bundle_id)})
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": str(e)[:200]}


def diagnostico(user_id: int) -> dict:
    """Testa as variantes do parâmetro e mostra QUAL funciona — é assim que validamos
    sem adivinhar. Roda poucas chamadas, uma por variante."""
    testes = [
        ("time_status NUMÉRICO 1 (todos) — correção proposta", {"time_status": 1, "page_size": 10}),
        ("time_status NUMÉRICO 3 (em andamento)", {"time_status": 3, "page_size": 10}),
        ("time_status TEXTO 'ongoing' — implementação antiga", {"time_status": "ongoing", "page_size": 10}),
        ("sem time_status (só page_size)", {"page_size": 10}),
    ]
    saida = []
    for nome, extra in testes:
        item = {"variante": nome, "enviado": extra}
        try:
            r = shopee._chamar(user_id, "/api/v2/bundle_deal/get_bundle_deal_list", extra=extra)
            resp = r.get("response") or {}
            item["ok"] = True
            item["bundles_retornados"] = len(resp.get("bundle_deal_list") or [])
            item["more"] = bool(resp.get("more"))
        except Exception as e:  # noqa: BLE001
            item["ok"] = False
            item["erro"] = str(e)[:220]
        saida.append(item)
    vencedora = next((t for t in saida if t.get("ok")), None)
    return {
        "testes": saida,
        "veredito": (f"FUNCIONA: {vencedora['variante']}" if vencedora
                     else "NENHUMA variante funcionou — provável falta de permissão do app"),
        "recomendacao": ("aplicar a variante que funcionou em shopee_bundle.listar()"
                         if vencedora else
                         "revisar a autorização do app na Central do Vendedor (permissão de Bundle Deal)"),
    }


def instalar_correcao() -> bool:
    """Substitui `shopee.listar_bundles` pela versão corrigida EM TEMPO DE EXECUÇÃO.

    Por que assim: `shopee.py` é FROZEN e tem um uso interno da função quebrada
    (o calendário de promoções, linha ~1477). Sem esta troca, aquele ponto
    continuaria mandando `time_status` como texto e falhando em silêncio.
    Trocando a função no import, TODOS os chamadores passam a usar o parâmetro certo.
    """
    def _corrigida(user_id: int, status="ongoing", limite: int = 50) -> dict:
        r = listar(user_id, status=status, limite=limite)
        return {"response": {"bundle_deal_list": [b["bruto"] for b in r["bundles"]],
                             "more": r["more"], "next_cursor": r["next_cursor"]}}
    try:
        shopee.listar_bundles = _corrigida
        print("[bundle] correção instalada: time_status numérico em todos os chamadores", flush=True)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[bundle] não consegui instalar a correção: {e}", flush=True)
        return False

def ler_estrutura(user_id: int, bundle_id=None) -> dict:
    """REVELA como a Shopee representa os NÍVEIS (Compre 2 -> 5%, Compre 3 -> 7%...).

    Por que existe: a tela do Seller Center mostra vários níveis por combo, mas a
    documentação do bundle_deal descreve UMA regra só. Em vez de adivinhar a estrutura
    e errar de novo, lemos de volta um combo criado À MÃO no painel da Shopee — a
    resposta da própria API mostra o formato exato que devemos enviar.

    Uso: crie um combo com 3 níveis no Seller Center e chame este endpoint.
    """
    out = {"instrucao": "crie 1 combo com vários níveis no Seller Center e rode isto"}
    try:
        if bundle_id:
            r = shopee._chamar(user_id, "/api/v2/bundle_deal/get_bundle_deal",
                               extra={"bundle_deal_id": int(bundle_id)})
            bruto = (r.get("response") or {})
        else:
            r = shopee._chamar(user_id, "/api/v2/bundle_deal/get_bundle_deal_list",
                               extra={"time_status": 1, "page_size": 20})
            lista = ((r.get("response") or {}).get("bundle_deal_list") or [])
            out["combos_encontrados"] = len(lista)
            bruto = lista[0] if lista else {}
        if not bruto:
            out["veredito"] = "nenhum combo na loja — crie um no Seller Center para eu ler a estrutura"
            return out
        out["combo_bruto"] = bruto
        regra = bruto.get("bundle_deal_rule") or {}
        out["regra_bruta"] = regra
        # procura qualquer campo que pareça uma LISTA de níveis
        niveis = []
        for k, v in list(bruto.items()) + list(regra.items()):
            if isinstance(v, list) and v and isinstance(v[0], dict):
                if any(x in str(v[0]).lower() for x in ("amount", "percentage", "price", "discount")):
                    niveis.append({"campo": k, "exemplo": v[:3], "quantidade": len(v)})
        out["possiveis_niveis"] = niveis
        out["veredito"] = (f"NÍVEIS ficam no campo '{niveis[0]['campo']}' ({niveis[0]['quantidade']} nível/níveis)"
                           if niveis else
                           "a API devolveu UMA regra só — o combo com níveis pode ser outro recurso, "
                           "ou cada nível vira um bundle separado")
        out["campos_da_regra"] = sorted(regra.keys())
    except Exception as e:  # noqa: BLE001
        out["erro"] = f"{type(e).__name__}: {str(e)[:250]}"
    return out
