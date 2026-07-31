"""Prova de Expedição — Mesa de Separação (mockup v3 aprovado).

Cada pedido conferido vira um DOSSIÊ:
  · sessão aberta pela bipagem da etiqueta;
  · takes (imagens JPEG já com os dados do pedido QUEIMADOS no pixel pelo navegador);
  · linha do tempo com bipagens, tiques manuais e divergências;
  · CADEIA DE HASH: o elo de cada take inclui o elo anterior — trocar, editar ou
    apagar qualquer imagem depois quebra a cadeia, e a verificação acusa.

As imagens ficam em `sep_midia` (tabela própria, isolada) para não pesar nas
consultas do painel de pedidos.
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from .models import SepSessao, SepMidia, SepEvento

# roteiro obrigatório (mockup v3): sem os 5, o dossiê não sela
ROTEIRO = ["abertura", "bancada", "conferencia", "embalado", "etiqueta"]
PASSO_LABEL = {
    "abertura": "Abertura · pedido bipado",
    "bancada": "Itens na bancada",
    "conferencia": "Conferência dos SKUs",
    "embalado": "Tudo embalado, antes de fechar",
    "etiqueta": "Etiqueta colada e legível",
    "fechamento": "Volume fechado",
    "avulso": "Take avulso",
}
GENESE = "0" * 64


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _elo(hash_anterior: str, sha_img: str, passo: str, quando: str) -> str:
    """Elo da cadeia: amarra a imagem ao que veio antes e ao seu momento."""
    base = f"{hash_anterior}|{sha_img}|{passo}|{quando}".encode("utf-8")
    return _sha(base)


def proximo_codigo(db, user_id: int) -> str:
    n = db.query(SepSessao).filter(SepSessao.user_id == user_id).count()
    return f"SEP-{8000 + n + 1}"


def abrir(db, user_id: int, pedido: dict, bancada: str = "Bancada 1",
          operador: str = None, qualidade: str = "padrao") -> SepSessao:
    """Abre a sessão (a bipagem da etiqueta chama isto). Se já houver uma aberta
    para o mesmo pedido, devolve a existente — bipar duas vezes não duplica."""
    pid = str(pedido.get("pedido_id") or pedido.get("id") or "").strip()
    if not pid:
        raise ValueError("pedido sem identificador")
    ja = (db.query(SepSessao)
          .filter(SepSessao.user_id == user_id, SepSessao.pedido_id == pid,
                  SepSessao.estado == "aberta").first())
    if ja:
        return ja
    s = SepSessao(
        user_id=user_id, codigo=proximo_codigo(db, user_id),
        canal=(pedido.get("canal") or "ml"), pedido_id=pid,
        cliente=pedido.get("cliente"), cliente_doc=pedido.get("cliente_doc"),
        cidade=pedido.get("cidade"), uf=pedido.get("uf"),
        nfe_numero=pedido.get("nfe_numero"), rastreio=pedido.get("rastreio"),
        valor=pedido.get("valor"), itens=pedido.get("itens") or [],
        bancada=bancada, operador=operador, qualidade=qualidade,
        aberta_em=datetime.utcnow(), estado="aberta", integra=True, bytes_total=0,
    )
    db.add(s)
    db.flush()
    db.add(SepEvento(user_id=user_id, sessao_id=s.id, tipo="abertura",
                     descricao=f"sessão aberta pela bipagem do pedido {pid}",
                     criado_em=datetime.utcnow()))
    db.commit()
    db.refresh(s)
    return s


def ultimo_elo(db, user_id: int, sessao_id: int) -> str:
    m = (db.query(SepMidia)
         .filter(SepMidia.user_id == user_id, SepMidia.sessao_id == sessao_id)
         .order_by(SepMidia.ordem.desc()).first())
    return m.hash_elo if m else GENESE


def add_take(db, user_id: int, sessao_id: int, dados: bytes, passo: str = "avulso",
             modo: str = "auto", gatilho: str = None, largura: int = None,
             altura: int = None, mime: str = "image/jpeg") -> SepMidia:
    """Grava um take e o encadeia na cadeia de hash."""
    s = (db.query(SepSessao)
         .filter(SepSessao.user_id == user_id, SepSessao.id == sessao_id).first())
    if s is None:
        raise ValueError("sessão não encontrada")
    if s.estado != "aberta":
        raise ValueError("sessão já selada — não aceita novos takes")
    if not dados:
        raise ValueError("take vazio")
    ordem = (db.query(SepMidia)
             .filter(SepMidia.user_id == user_id, SepMidia.sessao_id == sessao_id)
             .count()) + 1
    sha_img = _sha(dados)
    anterior = ultimo_elo(db, user_id, sessao_id)
    quando = datetime.utcnow().isoformat()
    m = SepMidia(
        user_id=user_id, sessao_id=sessao_id, ordem=ordem, passo=passo, modo=modo,
        gatilho=gatilho, mime=mime, largura=largura, altura=altura,
        bytes=len(dados), dados=dados, sha256=sha_img,
        hash_anterior=anterior, hash_elo=_elo(anterior, sha_img, passo, quando),
        criada_em=datetime.utcnow(),
    )
    db.add(m)
    s.bytes_total = (s.bytes_total or 0) + len(dados)
    db.flush()
    db.add(SepEvento(user_id=user_id, sessao_id=sessao_id, tipo="take",
                     descricao=f"{PASSO_LABEL.get(passo, passo)} · {modo}",
                     dados={"gatilho": gatilho, "bytes": len(dados)},
                     midia_id=m.id, criado_em=datetime.utcnow()))
    db.commit()
    db.refresh(m)
    return m


def evento(db, user_id: int, sessao_id: int, tipo: str, descricao: str = None,
           sku: str = None, dados: dict = None) -> SepEvento:
    e = SepEvento(user_id=user_id, sessao_id=sessao_id, tipo=tipo,
                  descricao=descricao, sku=sku, dados=dados,
                  criado_em=datetime.utcnow())
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


def passos_feitos(db, user_id: int, sessao_id: int) -> set:
    linhas = (db.query(SepMidia.passo)
              .filter(SepMidia.user_id == user_id, SepMidia.sessao_id == sessao_id).all())
    return {p[0] for p in linhas if p[0]}


def selar(db, user_id: int, sessao_id: int, forcar: bool = False) -> dict:
    """Sela o dossiê. Sem os 5 passos do roteiro, NÃO sela (regra do cliente)."""
    s = (db.query(SepSessao)
         .filter(SepSessao.user_id == user_id, SepSessao.id == sessao_id).first())
    if s is None:
        raise ValueError("sessão não encontrada")
    if s.estado == "selada":
        return {"ok": True, "ja_selada": True, "codigo": s.codigo}
    feitos = passos_feitos(db, user_id, sessao_id)
    faltam = [p for p in ROTEIRO if p not in feitos]
    if faltam and not forcar:
        return {"ok": False, "faltam": faltam,
                "erro": "roteiro incompleto: " + ", ".join(PASSO_LABEL.get(p, p) for p in faltam)}
    s.estado = "selada"
    s.selada_em = datetime.utcnow()
    if s.aberta_em:
        s.duracao_seg = int((s.selada_em - s.aberta_em).total_seconds())
    s.hash_final = ultimo_elo(db, user_id, sessao_id)
    db.add(SepEvento(user_id=user_id, sessao_id=sessao_id, tipo="selagem",
                     descricao=f"dossiê selado · hash final {s.hash_final[:12]}",
                     dados={"faltavam": faltam} if faltam else None,
                     criado_em=datetime.utcnow()))
    db.commit()
    return {"ok": True, "codigo": s.codigo, "hash_final": s.hash_final,
            "duracao_seg": s.duracao_seg, "bytes": s.bytes_total,
            "forcado": bool(faltam and forcar)}


def verificar(db, user_id: int, sessao_id: int) -> dict:
    """Recalcula a cadeia inteira. É isto que prova que nada foi trocado depois."""
    midias = (db.query(SepMidia)
              .filter(SepMidia.user_id == user_id, SepMidia.sessao_id == sessao_id)
              .order_by(SepMidia.ordem.asc()).all())
    anterior = GENESE
    quebras = []
    for m in midias:
        sha_real = _sha(m.dados or b"")
        if sha_real != m.sha256:
            quebras.append({"ordem": m.ordem, "passo": m.passo,
                            "motivo": "o conteúdo da imagem não bate com a impressão digital registrada"})
        if (m.hash_anterior or GENESE) != anterior:
            quebras.append({"ordem": m.ordem, "passo": m.passo,
                            "motivo": "o elo não aponta para a imagem anterior — houve remoção ou reordenação"})
        anterior = m.hash_elo
    s = (db.query(SepSessao)
         .filter(SepSessao.user_id == user_id, SepSessao.id == sessao_id).first())
    integra = not quebras
    if s is not None and s.integra != integra:
        s.integra = integra
        db.commit()
    return {"integra": integra, "takes": len(midias), "quebras": quebras}


def dossie(db, user_id: int, sessao_id: int) -> dict:
    s = (db.query(SepSessao)
         .filter(SepSessao.user_id == user_id, SepSessao.id == sessao_id).first())
    if s is None:
        return {}
    midias = (db.query(SepMidia)
              .filter(SepMidia.user_id == user_id, SepMidia.sessao_id == sessao_id)
              .order_by(SepMidia.ordem.asc()).all())
    eventos = (db.query(SepEvento)
               .filter(SepEvento.user_id == user_id, SepEvento.sessao_id == sessao_id)
               .order_by(SepEvento.criado_em.asc()).all())
    return {
        "sessao": resumo(s),
        "takes": [{
            "id": m.id, "ordem": m.ordem, "passo": m.passo,
            "label": PASSO_LABEL.get(m.passo, m.passo), "modo": m.modo,
            "gatilho": m.gatilho, "bytes": m.bytes, "kb": round((m.bytes or 0) / 1024, 1),
            "largura": m.largura, "altura": m.altura,
            "sha256": m.sha256, "hash_anterior": m.hash_anterior, "hash_elo": m.hash_elo,
            "criada_em": m.criada_em.isoformat() if m.criada_em else None,
            "url": f"/api/separacao/midia/{m.id}",
        } for m in midias],
        "eventos": [{
            "tipo": e.tipo, "descricao": e.descricao, "sku": e.sku, "dados": e.dados,
            "quando": e.criado_em.isoformat() if e.criado_em else None,
        } for e in eventos],
        "roteiro": {"exigido": ROTEIRO,
                    "feitos": sorted(passos_feitos(db, user_id, sessao_id)),
                    "faltam": [p for p in ROTEIRO if p not in passos_feitos(db, user_id, sessao_id)]},
    }


def resumo(s: SepSessao) -> dict:
    return {
        "id": s.id, "codigo": s.codigo, "canal": s.canal, "pedido_id": s.pedido_id,
        "cliente": s.cliente, "cliente_doc": s.cliente_doc, "cidade": s.cidade, "uf": s.uf,
        "nfe_numero": s.nfe_numero, "rastreio": s.rastreio, "valor": s.valor,
        "itens": s.itens or [], "bancada": s.bancada, "operador": s.operador,
        "qualidade": s.qualidade, "estado": s.estado, "integra": s.integra,
        "aberta_em": s.aberta_em.isoformat() if s.aberta_em else None,
        "selada_em": s.selada_em.isoformat() if s.selada_em else None,
        "duracao_seg": s.duracao_seg, "bytes_total": s.bytes_total or 0,
        "kb_total": round((s.bytes_total or 0) / 1024, 1),
        "hash_final": s.hash_final, "usada_em_disputa": s.usada_em_disputa,
    }


def listar(db, user_id: int, busca: str = "", limite: int = 50) -> list:
    q = db.query(SepSessao).filter(SepSessao.user_id == user_id)
    if busca:
        t = f"%{busca.strip()}%"
        q = q.filter((SepSessao.pedido_id.ilike(t)) | (SepSessao.cliente.ilike(t))
                     | (SepSessao.codigo.ilike(t)) | (SepSessao.nfe_numero.ilike(t))
                     | (SepSessao.rastreio.ilike(t)))
    return [resumo(s) for s in q.order_by(SepSessao.aberta_em.desc()).limit(limite).all()]


def estatisticas(db, user_id: int) -> dict:
    """KPIs do topo da estação."""
    from datetime import timedelta
    hoje = datetime.utcnow() - timedelta(hours=24)
    base = db.query(SepSessao).filter(SepSessao.user_id == user_id)
    do_dia = base.filter(SepSessao.aberta_em >= hoje).all()
    seladas = [s for s in do_dia if s.estado == "selada" and s.duracao_seg]
    med = int(sum(s.duracao_seg for s in seladas) / len(seladas)) if seladas else 0
    total_bytes = sum((s.bytes_total or 0) for s in base.all())
    autos = db.query(SepMidia).filter(SepMidia.user_id == user_id, SepMidia.modo == "auto").count()
    todos = db.query(SepMidia).filter(SepMidia.user_id == user_id).count()
    return {
        "separados_hoje": len([s for s in do_dia if s.estado == "selada"]),
        "abertas": len([s for s in do_dia if s.estado == "aberta"]),
        "tempo_medio_seg": med,
        "takes_auto_pct": round(autos / todos * 100) if todos else 0,
        "takes_total": todos,
        "integros": len([s for s in do_dia if s.integra]),
        "bytes_total": total_bytes,
        "gb_total": round(total_bytes / (1024 ** 3), 2),
        "bytes_hoje": sum((s.bytes_total or 0) for s in do_dia),
    }
