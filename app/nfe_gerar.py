"""Geração de NF-e a partir do pedido — via API pública do Bling (POST /nfe).

CONTEXTO (decidido com o cliente):
- Hoje a nota é gerada manualmente no Bling, que espelha o pedido de venda.
- A API pública NÃO tem um "gerar a partir do pedido"; tem POST /nfe (cria a nota,
  situação Pendente, SEM transmitir ao Sefaz) e POST /nfe/{id}/enviar (transmite).
- Escopo desta 1ª versão: apenas GERAR (Pendente). A transmissão continua no Bling.
- Estratégia de segurança: em vez de adivinhar o corpo, usamos uma NOTA-MOLDE — uma
  nota que o cliente já gerou — como base fiel dos campos que o Bling aceita, trocando
  destinatário e itens pelos do pedido. Isso reduce drasticamente o risco de rejeição.

`bling.py` e `nfe.py` são FROZEN — este módulo só REUSA `bling._request`, `bling.obter_nfe`
e `nfe.resumir_lista`. Nada é transmitido ao Sefaz aqui.
"""
from __future__ import annotations

from . import bling, nfe


class GeracaoErro(Exception):
    pass


def _molde(user_id: int, dias: int = 90) -> dict | None:
    """Pega a nota mais recente do cliente como MOLDE (estrutura que o Bling aceita).
    Preferimos uma já autorizada; se não houver, qualquer uma serve de referência."""
    from datetime import date, timedelta
    fim = date.today()
    ini = fim - timedelta(days=dias)
    try:
        raw = bling.listar_nfe(user_id, pagina=1, limite=20,
                               data_ini=ini.isoformat(), data_fim=fim.isoformat())
    except Exception as e:  # noqa: BLE001
        raise GeracaoErro(f"não consegui listar notas para usar de molde: {e}")
    linhas = nfe.resumir_lista(raw) or []
    if not linhas:
        return None
    # tenta uma autorizada primeiro (molde mais confiável)
    autorizadas = [x for x in linhas if str(x.get("situacao") or "").lower() in ("autorizada", "5", "authorized")]
    alvo = (autorizadas or linhas)[0]
    try:
        raw = bling.obter_nfe(user_id, alvo.get("id"))
    except Exception:  # noqa: BLE001
        return None
    # o Bling v3 devolve {"data": {...}} — sem desembrulhar, naturezaOperacao/loja somem
    return raw.get("data", raw) if isinstance(raw, dict) else None


def _rua_numero(completo: str, numero=None):
    """Separa logradouro e número. A NF-e exige campos distintos (xLgr / nro):
    "Rua Genebaldo Figueiredo 44" -> ("Rua Genebaldo Figueiredo", "44")."""
    import re
    txt = str(completo or "").strip()
    if numero:
        return txt or "", str(numero)
    m = re.search(r"^(.*?)[,\s]+(\d+[A-Za-z]?)\s*$", txt)
    if m:
        return m.group(1).strip(" ,-"), m.group(2)
    return txt, "S/N"


def _so_digitos(v) -> str:
    return "".join(ch for ch in str(v or "") if ch.isdigit())


def montar_corpo(molde: dict, pedido: dict) -> dict:
    """Monta o corpo do POST /nfe a partir de uma nota-molde + os dados do pedido.

    pedido = {
      "numero_loja": "...",            # numeroPedidoLoja (rastreio do casamento)
      "cliente": {nome, cpf_cnpj, ie, email, telefone,
                  endereco:{completo,numero,bairro,cidade,uf,cep}},
      "itens": [{sku, descricao, ncm, cfop, quantidade, valor}],
      "loja_id": <opcional, herda do molde se ausente>,
    }
    Campos fiscais dos ITENS (NCM/CFOP/origem) vêm do cadastro do produto no Bling —
    o cliente confirmou que estão corretos.
    """
    m = molde or {}
    cli = pedido.get("cliente") or {}
    end = cli.get("endereco") or {}

    contato = {
        "nome": cli.get("nome") or "Consumidor final",
        "numeroDocumento": _so_digitos(cli.get("cpf_cnpj")),
        "tipoPessoa": "J" if len(_so_digitos(cli.get("cpf_cnpj"))) > 11 else "F",
    }
    if cli.get("ie"):
        contato["ie"] = cli["ie"]
    if cli.get("email"):
        contato["email"] = cli["email"]
    if end:
        rua, num = _rua_numero(end.get("completo") or end.get("logradouro") or "", end.get("numero"))
        contato["endereco"] = {
            "endereco": rua,
            "numero": num,
            "bairro": end.get("bairro") or "",
            "cep": _so_digitos(end.get("cep")),
            "municipio": end.get("cidade") or "",
            "uf": (end.get("uf") or "")[:2].upper(),
        }

    itens = []
    for it in (pedido.get("itens") or []):
        item = {
            "codigo": str(it.get("sku") or ""),
            "descricao": it.get("descricao") or it.get("nome") or "Item",
            "quantidade": float(it.get("quantidade") or 1),
            "valor": float(it.get("valor") or it.get("preco") or 0),
        }
        # tributação vem do produto cadastrado; só enviamos se o pedido trouxe explícito
        if it.get("ncm"):
            item["ncm"] = _so_digitos(it["ncm"])
        if it.get("cfop"):
            item["cfop"] = _so_digitos(it["cfop"])
        itens.append(item)

    # herda do molde tudo que é config da empresa (loja, natureza, série, tipo, finalidade)
    corpo = {
        "tipo": m.get("tipo", 1),                       # 1 = saída
        "finalidade": m.get("finalidade", 1),           # 1 = normal
        "contato": contato,
        "itens": itens,
    }
    if pedido.get("numero_loja"):
        corpo["numeroPedidoLoja"] = str(pedido["numero_loja"])
    # loja / natureza de operação: essenciais e específicas da conta — herdadas do molde
    for campo in ("loja", "naturezaOperacao", "serie", "unidadeNegocio"):
        if isinstance(m.get(campo), dict) and m[campo].get("id"):
            corpo[campo] = {"id": m[campo]["id"]}
        elif m.get(campo) not in (None, ""):
            corpo[campo] = m[campo]
    if pedido.get("loja_id"):
        corpo["loja"] = {"id": pedido["loja_id"]}
    return corpo


def gerar(user_id: int, pedido: dict, molde: dict | None = None, dry_run: bool = True) -> dict:
    """Gera UMA nota (situação Pendente) via POST /nfe. NÃO transmite ao Sefaz.

    dry_run=True (padrão): monta e devolve o corpo, mas NÃO envia — para conferência.
    dry_run=False: cria a nota Pendente no Bling de fato.
    """
    if molde is None:
        molde = _molde(user_id)
    if not molde:
        return {"ok": False, "erro": "Nenhuma nota anterior encontrada para usar de molde. "
                "Gere 1 nota manualmente no Bling primeiro — ela vira o molde."}
    corpo = montar_corpo(molde, pedido)
    # validações mínimas antes de tocar na API
    faltas = []
    if not corpo.get("itens"):
        faltas.append("sem itens")
    if not (corpo.get("contato") or {}).get("numeroDocumento"):
        faltas.append("sem CPF/CNPJ do destinatário")
    if not corpo.get("naturezaOperacao") and not corpo.get("loja"):
        faltas.append("o molde não trouxe natureza de operação nem loja — "
                      "confira se a nota usada de molde está completa no Bling")
    if not (corpo.get("contato") or {}).get("endereco", {}).get("municipio"):
        faltas.append("sem cidade/UF do destinatário")
    if faltas:
        return {"ok": False, "erro": "não gerei: " + ", ".join(faltas), "corpo": corpo}

    if dry_run:
        return {"ok": True, "dry_run": True, "corpo": corpo,
                "resumo": f"{len((corpo.get('itens') or []))} item(ns) · destinatário {corpo['contato']['nome']}"}

    try:
        r = bling._request(user_id, "POST", "/nfe", json=corpo)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "erro": f"falha na chamada: {e}", "corpo": corpo}
    if r.status_code >= 400:
        try:
            det = r.json()
        except Exception:  # noqa: BLE001
            det = {"raw": r.text[:400]}
        return {"ok": False, "status": r.status_code, "erro_bling": det, "corpo": corpo}
    data = r.json()
    nota = (data or {}).get("data") or data
    return {"ok": True, "gerada": True, "nfe_id": nota.get("id"),
            "numero": nota.get("numero"), "situacao": nota.get("situacao"), "resposta": nota}
