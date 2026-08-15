from datetime import datetime, date

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Date, Float, Boolean, ForeignKey, UniqueConstraint, JSON,
    LargeBinary, Index
)

from .db import Base


class User(Base):
    """Cada usuário é um tenant isolado (sua própria conta Bling e seus dados)."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    nome = Column(String, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow)


class OAuthToken(Base):
    """Token do Bling POR usuário (1 linha por tenant)."""

    __tablename__ = "bling_oauth_token"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    access_token = Column(String, nullable=False)
    refresh_token = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AiUsage(Base):
    """Contador de uso da IA por usuário/dia (controle de custo comercial)."""

    __tablename__ = "ai_usage"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    dia = Column(Date, default=date.today, nullable=False)
    contador = Column(Integer, default=0, nullable=False)

    __table_args__ = (UniqueConstraint("user_id", "dia", name="uq_ai_usage_user_dia"),)


class NfeConfig(Base):
    """Config do módulo de NF-e por tenant: modo automático + regra padrão de edição."""

    __tablename__ = "nfe_config"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    auto = Column(Boolean, default=False, nullable=False)               # toggle do modo automático
    desconto_tipo = Column(String, default="percentual", nullable=False)  # 'percentual' | 'valor'
    desconto_valor = Column(Float, default=0.0, nullable=False)
    remover_frete = Column(Boolean, default=True, nullable=False)
    # Overrides de desconto por plataforma (JSON): {"Shopee": {"tipo": "percentual", "valor": 90}, ...}.
    # Quando a nota é de uma plataforma com override, o lote/automático usa essa regra no lugar da padrão.
    desconto_plataformas = Column(JSON, default=dict, nullable=True)
    # Código da situação "Pendente" na API do Bling. Na v3 costuma ser 1, mas deixamos
    # configurável para não arriscar erro fiscal caso a sua conta use outro código.
    situacao_pendente = Column(Integer, default=1, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class NfeFaturamentoMes(Base):
    """Snapshot mensal de faturamento (NF-e de saída autorizadas) por tenant, para o monitor
    do teto do Simples. Como a lista do Bling não traz o valor, guardamos contagem EXATA +
    total ESTIMADO por média amostral, recalculado sob demanda (job pesado em background)."""

    __tablename__ = "nfe_faturamento_mes"
    __table_args__ = (UniqueConstraint("user_id", "ano", "mes", name="uq_faturamento_mes"),)

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    ano = Column(Integer, nullable=False)
    mes = Column(Integer, nullable=False)
    qtd = Column(Integer, default=0, nullable=False)            # contagem exata de notas autorizadas (saída)
    amostra = Column(Integer, default=0, nullable=False)        # quantas notas foram lidas p/ a média
    total_estimado = Column(Float, default=0.0, nullable=False)
    parcial = Column(Boolean, default=False, nullable=False)    # True se o mês passou do teto de páginas
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RadarAlvo(Base):
    """Um anúncio de concorrente monitorado, por tenant e por SKU."""

    __tablename__ = "radar_alvo"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    sku = Column(String, nullable=False, index=True)        # SKU do nosso produto
    nome = Column(String, nullable=True)                    # nome da loja/concorrente
    marketplace = Column(String, nullable=True)             # ex.: mercadolivre, shopee
    url = Column(String, nullable=False)                    # link do anúncio do concorrente
    ativo = Column(Boolean, default=True, nullable=False)
    criado_em = Column(DateTime, default=datetime.utcnow)


class RadarSnapshot(Base):
    """Foto do preço de um alvo num instante. O histórico nasce do acúmulo destas."""

    __tablename__ = "radar_snapshot"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    alvo_id = Column(Integer, ForeignKey("radar_alvo.id"), nullable=False, index=True)
    preco_normal = Column(Float, nullable=True)
    preco_oferta = Column(Float, nullable=True)
    coletado_em = Column(DateTime, default=datetime.utcnow, index=True)


class PrecificacaoConfig(Base):
    """Configuração de precificação por tenant: custos globais + taxas por canal.

    A coluna `canais` guarda (JSON) a lista de canais, cada um com suas FAIXAS de preço:
    [{canal, nome, ativo, faixas:[{ate, comissao, fixo, fixo_pct}]}].
    `ate` = teto da faixa (None = sem teto / catch-all).
    """

    __tablename__ = "precificacao_config"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)

    # custos globais em % (incidem sobre o preço de venda)
    imposto = Column(Float, default=12.0, nullable=False)
    cartao = Column(Float, default=2.5, nullable=False)
    # custos por unidade em R$ (somados ao custo do produto)
    embalagem = Column(Float, default=0.0, nullable=False)
    frete = Column(Float, default=0.0, nullable=False)
    # margem líquida desejada padrão (%)
    margem_padrao = Column(Float, default=20.0, nullable=False)

    canais = Column(JSON, default=list)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OAuthState(Base):
    """State do OAuth do Bling, guardado no banco (uso único, TTL curto).

    Padrão correto de CSRF para OAuth: imune a redeploy e a troca de JWT_SECRET,
    e sem risco de truncamento (token curto em vez de um JWT longo no state).
    """

    __tablename__ = "oauth_state"

    state = Column(String, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    criado_em = Column(DateTime, default=datetime.utcnow, nullable=False)


class Notificacao(Base):
    """Notificação da plataforma gerada por QUALQUER módulo (NF-e, precificação, avaliações,
    radar de concorrência, agentes…). Alimenta o sino global. Separado do log de webhooks."""

    __tablename__ = "notificacao"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    categoria = Column(String, default="outro", nullable=False)  # nfe|precificacao|avaliacao|radar|concorrencia|pedido|estoque|agente|outro
    titulo = Column(String, nullable=False)
    texto = Column(String, nullable=True)
    ok = Column(Boolean, default=True, nullable=False)
    modulo = Column(String, nullable=True)
    entidade_id = Column(String, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    lido = Column(Boolean, default=False, nullable=False)


class WebhookEvento(Base):
    """Log dos eventos recebidos do Bling via webhook (push em tempo real)."""

    __tablename__ = "webhook_eventos"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    event = Column(String, nullable=True)        # ex.: "produto.updated"
    recurso = Column(String, nullable=True, index=True)  # ex.: "produto"
    acao = Column(String, nullable=True)         # ex.: "updated"
    event_id = Column(String, nullable=True, index=True)  # dedupe
    company_id = Column(String, nullable=True)
    entidade_id = Column(String, nullable=True)  # data.id
    payload = Column(JSON, nullable=True)
    processado = Column(Boolean, default=False)
    resultado = Column(JSON, nullable=True)      # resultado do processamento (ex.: auto-apply de NF-e)
    recebido_em = Column(DateTime, default=datetime.utcnow, index=True)


class ProdutoSync(Base):
    """Status de sincronização de um produto entre o app e o Bling.
    'enviado' quando empurramos uma alteração; 'confirmado' quando o webhook
    de produto.updated chega de volta. Pendente = enviado mas ainda não confirmado."""

    __tablename__ = "produto_sync"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    produto_id = Column(String, nullable=False, index=True)
    sku = Column(String, nullable=True)
    status = Column(String, default="enviado")     # enviado | confirmado | erro
    campos = Column(JSON, nullable=True)            # o que foi enviado por último
    enviado_em = Column(DateTime, nullable=True)
    confirmado_em = Column(DateTime, nullable=True)
    erro = Column(String, nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "produto_id", name="uq_sync_user_produto"),)


class ProdutoCache(Base):
    """Cópia local (cache) do catálogo do Bling. Carregado uma vez por completo e
    mantido atualizado via webhook — assim as telas leem daqui e o Bling fica com folga."""

    __tablename__ = "produto_cache"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    produto_id = Column(String, nullable=False, index=True)
    sku = Column(String, nullable=True, index=True)
    nome = Column(String, nullable=True)
    imagem = Column(String, nullable=True)
    preco = Column(Float, default=0.0)            # preço-base / líquido a receber
    custo = Column(Float, default=0.0)
    saldo = Column(Float, default=0.0)
    situacao = Column(String, nullable=True)   # Ativo / Inativo
    tipo = Column(String, nullable=True)
    marketplaces = Column(JSON, nullable=True)  # canais onde está anunciado (enriquecido sob demanda)
    dados = Column(JSON, nullable=True)        # payload bruto do produto
    atualizado_em = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("user_id", "produto_id", name="uq_cache_user_produto"),)


class ProdutoPrecoSnapshot(Base):
    """Histórico do Preço Bling (preço-base) por produto — um ponto por dia, gravado no
    sync. Alimenta o gráfico de histórico de preço no cockpit do Catálogo."""

    __tablename__ = "produto_preco_snapshot"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    produto_id = Column(String, nullable=False, index=True)
    sku = Column(String, nullable=True)
    preco = Column(Float, default=0.0)
    dia = Column(Date, index=True)
    criado_em = Column(DateTime, default=datetime.utcnow)


class VinculosSync(Base):
    """Estado do job de enriquecimento de vínculos (mapear canais por produto no Bling)."""

    __tablename__ = "vinculos_sync"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True, unique=True)
    status = Column(String, default="ocioso")   # ocioso/rodando/concluido/erro
    total = Column(Integer, default=0)
    processados = Column(Integer, default=0)
    erro = Column(String, nullable=True)
    iniciado_em = Column(DateTime, nullable=True)
    concluido_em = Column(DateTime, nullable=True)


class CatalogoSync(Base):
    """Estado da sincronização completa do catálogo (uma linha por usuário)."""

    __tablename__ = "catalogo_sync"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    status = Column(String, default="ocioso")   # ocioso | rodando | concluido | erro
    total = Column(Integer, default=0)           # total no cache
    paginas = Column(Integer, default=0)
    erro = Column(String, nullable=True)
    iniciado_em = Column(DateTime, nullable=True)
    concluido_em = Column(DateTime, nullable=True)


class ShopeeConta(Base):
    """Credenciais e tokens da Shopee por usuário (multi-tenant).
    O access_token expira em ~4h e é renovado pelo refresh_token automaticamente."""

    __tablename__ = "shopee_conta"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    shop_id = Column(String, nullable=True)
    access_token = Column(String, nullable=True)
    refresh_token = Column(String, nullable=True)
    expira_em = Column(DateTime, nullable=True)       # quando o access_token expira
    conectado_em = Column(DateTime, nullable=True)
    nome_loja = Column(String, nullable=True)
    ativo = Column(Boolean, default=True)


class ShopeeBoostItem(Base):
    """Produto na lista de auto-boost rotativo da Shopee.
    fixo=True => sempre impulsionado (pin, máx 5). Senão entra no rodízio por prioridade."""

    __tablename__ = "shopee_boost_item"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    item_id = Column(String, nullable=False)          # id do anúncio na Shopee
    nome = Column(String, nullable=True)
    fixo = Column(Boolean, default=False)             # pin
    prioridade = Column(Integer, default=0)           # maior = impulsiona antes
    ultimo_boost = Column(DateTime, nullable=True)    # quando foi impulsionado por último
    boost_ate = Column(DateTime, nullable=True)       # fim das 4h do boost atual
    impulsos = Column(Integer, default=0)             # contador de quantas vezes
    auto = Column(Boolean, default=False)             # entrou pela auto-seleção (vs manual)
    condicional = Column(Boolean, default=False)      # fixado pelo Radar (concorrente furou preço)


class ShopeeBoostLog(Base):
    """Histórico DURÁVEL de cada boost (auto/manual/radar). Base para a atribuição de vendas:
    cada linha é uma janela [inicio, fim] em que um produto ficou em destaque."""

    __tablename__ = "shopee_boost_log"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    item_id = Column(String, nullable=False, index=True)
    nome = Column(String, nullable=True)
    tipo = Column(String, default="auto")                 # auto | manual | radar
    inicio = Column(DateTime, default=datetime.utcnow, index=True)
    fim = Column(DateTime, nullable=True)                 # inicio + 4h
    vendas_atribuidas = Column(Integer, nullable=True)    # unidades vendidas na janela (atribuição)
    atribuido_em = Column(DateTime, nullable=True)
    motivo = Column(String, nullable=True)            # por que está em boost condicional
    criado_em = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("user_id", "item_id", name="uq_boost_user_item"),)


class ShopeeBoostConfig(Base):
    """Configuração do motor de auto-boost por usuário."""

    __tablename__ = "shopee_boost_config"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    ativo = Column(Boolean, default=False)            # liga/desliga o rodízio
    janela_inicio = Column(Integer, default=0)        # hora 0-23 (0 = sempre)
    janela_fim = Column(Integer, default=0)           # hora 0-23 (0 = sempre)
    criterio = Column(String, default="prioridade")   # prioridade | margem | giro | abc
    max_simultaneos = Column(Integer, default=5)      # teto da Shopee
    auto_selecao = Column(Boolean, default=False)     # agentes escolhem os produtos sozinhos
    auto_estrategia = Column(String, default="estoque_parado")  # estoque_parado | margem
    auto_maximo = Column(Integer, default=30)         # quantos manter na fila automática
    cond_ativo = Column(Boolean, default=False)       # boost condicional pelo Radar
    cond_gatilho_pct = Column(Float, default=0.0)     # concorrente X% mais barato dispara (0 = qualquer)
    cond_max = Column(Integer, default=3)             # máx itens em boost condicional ao mesmo tempo
    janelas = Column(JSON, nullable=True)             # [[ini,fim],...] janelas de pico (sobrepõe janela_inicio/fim)
    cond_estoque = Column(Boolean, default=False)     # condicional: empurrar antes de esgotar (estoque baixo + giro alto)
    cond_surto = Column(Boolean, default=False)       # condicional: surfar surto de vendas
    atualizado_em = Column(DateTime, default=datetime.utcnow)


class ShopeeReviewConfig(Base):
    """Como a IA lê e responde as avaliações da Shopee — no padrão da loja.
    modo=manual: a IA sugere e você revisa/edita antes de enviar.
    modo=auto: o agente responde sozinho as notas configuradas em auto_estrelas."""

    __tablename__ = "shopee_review_config"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    modo = Column(String, default="manual")            # manual | auto
    tom = Column(String, default="caloroso")           # caloroso | profissional | descontraido
    limite_chars = Column(Integer, default=450)        # teto do tamanho da resposta
    assinatura = Column(String, default="")            # ex.: "Equipe Sóstrass" (entra no fim)
    saudacao = Column(String, default="")              # ex.: "Oi, {nome}!" — opcional
    instrucoes = Column(String, default="")            # regras livres da loja
    oferecer_chat = Column(Boolean, default=True)      # em nota baixa, oferecer resolver pelo chat
    usar_nome = Column(Boolean, default=True)          # citar o nome do comprador
    usar_emoji = Column(Boolean, default=True)         # permitir emojis leves
    auto_estrelas = Column(JSON, default=lambda: [4, 5])  # quais notas o agente responde sozinho
    auto_pausa_seg = Column(Integer, default=5)        # pausa entre respostas (anti-flood na API)
    auto_max_ciclo = Column(Integer, default=10)       # máx. de respostas por ciclo do agendador
    emoji_intensidade = Column(String, default="leve")  # nenhum | leve | animado
    instrucoes_elogio = Column(String, default="")     # estratégia da IA p/ 4-5★
    instrucoes_morna = Column(String, default="")      # estratégia da IA p/ 3★
    instrucoes_critica = Column(String, default="")    # estratégia da IA p/ 1-2★
    frases_casa = Column(JSON, default=lambda: [])     # frases que a IA PODE usar (da casa)
    frases_proibidas = Column(JSON, default=lambda: [])  # frases que a IA JAMAIS diz (bloqueio duro)
    cupom_ativo = Column(Boolean, default=False)       # citar cupom de recompra nas respostas
    cupom_codigo = Column(String, default="")          # ex.: VOLTA5
    cupom_quando = Column(String, default="vips")      # vips | todas5 | nunca
    atualizado_em = Column(DateTime, default=datetime.utcnow)


class ShopeePromoConfig(Base):
    """Regras do motor de promoções automáticas (Shopee).
    modo=sugerir: o agente monta propostas e você aprova.
    modo=auto: o agente cria desconto/flash sozinho dentro das regras."""

    __tablename__ = "shopee_promo_config"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    ativo = Column(Boolean, default=False)
    modo = Column(String, default="auto")               # auto | sugerir  (padrão: agentes fazem)
    gatilho = Column(String, default="agendado")        # agendado | queda
    base_comparacao = Column(String, default="dia")     # dia | horario  (como medir a queda)
    dias_analise = Column(Integer, default=30)           # janela (dias) p/ medir vendas (estoque parado)
    estrategia = Column(String, default="estoque_parado")  # estoque_parado | margem_alta
    tipo = Column(String, default="desconto")           # desconto | flash | ambos
    desconto_max = Column(Integer, default=15)          # teto do desconto (%)
    piso_margem = Column(Float, default=10.0)           # nunca descontar abaixo desta margem (%)
    max_produtos = Column(Integer, default=20)          # itens por campanha
    estoque_minimo = Column(Integer, default=3)         # só promove com estoque >= isso
    reserva_estoque = Column(Integer, default=1)        # no flash, segura N unidades fora da oferta
    duracao_dias = Column(Integer, default=3)           # duração da campanha de desconto
    intervalo_dias = Column(Integer, default=7)         # no gatilho agendado
    queda_limiar = Column(Integer, default=30)          # % de queda de pedidos que dispara
    extras = Column(Text, nullable=True)                # JSON: latências do flash + agentes por vendas
    ultimo_ciclo = Column(DateTime, nullable=True)
    atualizado_em = Column(DateTime, default=datetime.utcnow)


class ShopeeImpressaoConfig(Base):
    """Dados do emitente + o que aparece nas impressões (folha de separação e etiqueta).
    Multi-tenant: cada conta personaliza o seu cabeçalho e os campos visíveis."""

    __tablename__ = "shopee_impressao_config"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    # Emitente (remetente) — sai no cabeçalho da folha e no bloco REMETENTE da etiqueta
    emitente_nome = Column(String, default="")
    emitente_cnpj = Column(String, default="")
    emitente_endereco = Column(String, default="")     # ex.: "Rua Comendador, 120"
    emitente_cidade = Column(String, default="")        # ex.: "Limeira - SP · CEP 13480-000"
    # O que mostrar
    mostrar_timeline = Column(Boolean, default=True)
    mostrar_nfe = Column(Boolean, default=True)
    mostrar_rastreio = Column(Boolean, default=True)
    mostrar_destinatario = Column(Boolean, default=True)
    mostrar_miniaturas = Column(Boolean, default=True)
    mostrar_complemento = Column(Boolean, default=True)
    mostrar_nota_comprador = Column(Boolean, default=True)
    mostrar_codigo_barras = Column(Boolean, default=True)
    mostrar_qr = Column(Boolean, default=True)
    atualizado_em = Column(DateTime, default=datetime.utcnow)


class ShopeeVendaSnapshot(Base):
    """Fotografia periódica de pedidos para detectar queda de vendas — total do dia
    e da janela de 6h, com a faixa de horário (bucket) para comparar mesmo horário."""

    __tablename__ = "shopee_venda_snapshot"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    pedidos_24h = Column(Integer, default=0)       # pedidos nas últimas 24h
    pedidos_6h = Column(Integer, default=0)        # pedidos na janela de 6h
    bucket = Column(Integer, default=0)            # faixa do dia: 0=madrugada 1=manhã 2=tarde 3=noite
    criado_em = Column(DateTime, default=datetime.utcnow, index=True)


class ShopeePromoLog(Base):
    """Histórico do que o motor criou (auditoria)."""

    __tablename__ = "shopee_promo_log"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tipo = Column(String)            # desconto | flash
    ref_id = Column(String)          # discount_id ou flash_sale_id
    nome = Column(String)
    qtd_itens = Column(Integer, default=0)
    desconto_pct = Column(Integer, default=0)
    motivo = Column(String)          # agendado | queda | manual
    criado_em = Column(DateTime, default=datetime.utcnow, index=True)


class ShopeeReviewLog(Base):
    """Auditoria das respostas de avaliação — alimenta o painel de atividade do agente."""

    __tablename__ = "shopee_review_log"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    comment_id = Column(String, index=True)
    nota = Column(Integer, default=0)
    buyer = Column(String, default="")
    produto = Column(String, default="")
    trecho = Column(String, default="")        # começo da resposta enviada
    modo = Column(String, default="auto")      # auto | manual
    criado_em = Column(DateTime, default=datetime.utcnow, index=True)


class ShopeeItemCache(Base):
    """Cache local dos anúncios da Shopee (sku -> item_id, preço, promoção...).
    Alimenta o cockpit (promoção) e acelera a divergência, sem martelar a API."""

    __tablename__ = "shopee_item_cache"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    item_id = Column(String, nullable=False, index=True)
    sku = Column(String, nullable=True, index=True)
    nome = Column(String, nullable=True)
    preco = Column(Float, default=0.0)            # preço atual (pode ser promo)
    preco_original = Column(Float, default=0.0)   # preço normal/cheio
    em_promocao = Column(Boolean, default=False)
    promo_nome = Column(String, nullable=True)
    imagem = Column(String, nullable=True)
    status = Column(String, nullable=True)
    atualizado_em = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("user_id", "item_id", name="uq_shopee_user_item"),)


class ShopeeSync(Base):
    """Estado da sincronização do catálogo da Shopee (uma linha por usuário)."""

    __tablename__ = "shopee_sync"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    status = Column(String, default="ocioso")   # ocioso | rodando | concluido | erro
    total = Column(Integer, default=0)
    erro = Column(String, nullable=True)
    iniciado_em = Column(DateTime, nullable=True)
    concluido_em = Column(DateTime, nullable=True)


class KpiSnapshot(Base):
    """Foto diária dos KPIs do catálogo (1 linha por dia por usuário). O acúmulo
    alimenta as setas de tendência e os sparklines do topo do Catálogo."""

    __tablename__ = "kpi_snapshot"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    dia = Column(Date, nullable=False, index=True)
    total = Column(Integer, default=0)
    saudavel = Column(Integer, default=0)
    atencao = Column(Integer, default=0)
    prejuizo = Column(Integer, default=0)
    sem_custo = Column(Integer, default=0)
    val_estoque = Column(Float, default=0.0)
    marg_media = Column(Float, nullable=True)
    cobertura = Column(JSON, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("user_id", "dia", name="uq_kpi_user_dia"),)


class MLConta(Base):
    """Credenciais/tokens do Mercado Livre por usuário (multi-tenant).
    O access_token expira em ~6h e é renovado pelo refresh_token automaticamente.
    Fallback single-tenant: se não houver linha, o módulo usa ML_* do ambiente."""

    __tablename__ = "ml_conta"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    seller_id = Column(String, nullable=True)
    nickname = Column(String, nullable=True)
    site_id = Column(String, default="MLB", nullable=True)
    access_token = Column(String, nullable=True)
    refresh_token = Column(String, nullable=True)
    expira_em = Column(DateTime, nullable=True)        # quando o access_token expira
    conectado_em = Column(DateTime, nullable=True)
    ativo = Column(Boolean, default=True)


class MLItemCache(Base):
    """Cache local dos anúncios do Mercado Livre (sku -> item_id, preço, status...).
    Alimenta o cockpit e respeita o limite de 1500 req/min (lê daqui, não da API)."""

    __tablename__ = "ml_item_cache"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    item_id = Column(String, nullable=False, index=True)
    sku = Column(String, nullable=True, index=True)
    titulo = Column(String, nullable=True)
    preco = Column(Float, default=0.0)
    preco_original = Column(Float, default=0.0)
    status = Column(String, nullable=True)               # active | paused | closed
    sub_status = Column(String, nullable=True)           # motivos (moderação/pausa), separados por vírgula
    estoque = Column(Integer, nullable=True)
    category_id = Column(String, nullable=True)
    listing_type_id = Column(String, nullable=True)      # free | gold_special | gold_pro
    logistic_type = Column(String, nullable=True)        # me1 | me2 | self_service | fulfillment | drop_off
    saude = Column(Float, nullable=True)                 # health (0-1) quando disponível
    permalink = Column(String, nullable=True)
    imagem = Column(String, nullable=True)
    em_promocao = Column(Boolean, default=False)
    dados = Column(JSON, nullable=True)                  # payload bruto reduzido
    atualizado_em = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("user_id", "item_id", name="uq_ml_user_item"),)


class MLSync(Base):
    """Estado da sincronização completa do catálogo do Mercado Livre (1 linha por usuário)."""

    __tablename__ = "ml_sync"

    user_id = Column(Integer, ForeignKey("users.id"), primary_key=True)
    status = Column(String, default="ocioso")   # ocioso | rodando | concluido | erro
    total = Column(Integer, default=0)
    processados = Column(Integer, default=0)
    erro = Column(String, nullable=True)
    iniciado_em = Column(DateTime, nullable=True)
    concluido_em = Column(DateTime, nullable=True)


class MLWebhookEvento(Base):
    """Log das notificações (webhooks) do Mercado Livre — alimenta o painel de
    sincronização em tempo real (eventos por tópico, latência, taxa de processamento)."""

    __tablename__ = "ml_webhook_evento"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    topic = Column(String, nullable=True, index=True)      # items | items_prices | stock_locations | ...
    resource = Column(String, nullable=True)               # ex.: /items/MLB123
    resource_id = Column(String, nullable=True, index=True)
    attempts = Column(Integer, default=1)
    processado = Column(Boolean, default=False)
    resultado = Column(String, nullable=True)
    recebido_em = Column(DateTime, default=datetime.utcnow, index=True)


class MLEnvioCache(Base):
    """Cache do estado de cada envio (shipment) do Mercado Livre.

    Alimentado por webhooks do tópico `shipments` (tempo real) e por backfill
    sob demanda. É a fonte de verdade dos baldes do painel (a despachar hoje,
    próximos dias, em trânsito, finalizadas), do prazo de coleta, rastreio e
    custos — dados que o /orders/search NÃO devolve.
    """

    __tablename__ = "ml_envio_cache"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    shipment_id = Column(String, nullable=False, index=True)
    order_id = Column(String, nullable=True, index=True)

    status = Column(String, nullable=True, index=True)
    substatus = Column(String, nullable=True)
    logistic_type = Column(String, nullable=True)
    mode = Column(String, nullable=True)

    handling_limit = Column(DateTime, nullable=True)      # prazo p/ despachar (coleta/manuseio)
    delivery_limit = Column(DateTime, nullable=True)      # previsão-limite de entrega
    date_ready = Column(DateTime, nullable=True)
    date_shipped = Column(DateTime, nullable=True)
    date_delivered = Column(DateTime, nullable=True)

    tracking_number = Column(String, nullable=True)
    tracking_method = Column(String, nullable=True)

    custo_vendedor = Column(Float, nullable=True)         # frete pago pelo vendedor
    custo_comprador = Column(Float, nullable=True)        # frete pago pelo comprador

    receiver_nome = Column(String, nullable=True)
    receiver_endereco = Column(String, nullable=True)     # linha compacta p/ lista
    receiver_cidade = Column(String, nullable=True)
    receiver_estado = Column(String, nullable=True)
    receiver_cep = Column(String, nullable=True)

    fiscal_pendente = Column(Boolean, default=False)
    devolucao = Column(Boolean, default=False)

    dados = Column(JSON, nullable=True)                   # shipment cru (x-format-new)
    atualizado_em = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("user_id", "shipment_id", name="uq_ml_user_shipment"),)


class MLPedidoCache(Base):
    """Cache de pedidos do Mercado Livre (nível pedido) — alimenta análise de vendas."""

    __tablename__ = "ml_pedido_cache"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    order_id = Column(String, nullable=False, index=True)
    pack_id = Column(String, nullable=True)
    status = Column(String, nullable=True, index=True)       # paid | cancelled | ...
    date_created = Column(DateTime, nullable=True, index=True)
    date_closed = Column(DateTime, nullable=True)
    total_amount = Column(Float, default=0.0)
    paid_amount = Column(Float, default=0.0)
    currency_id = Column(String, nullable=True)
    unidades = Column(Integer, default=0)
    itens = Column(JSON, nullable=True)                      # [{item_id,sku,titulo,quantidade,unit_price,sale_fee}]
    raw = Column(JSON, nullable=True)                        # payload cru do /orders/search — fonte do painel de pedidos
    atualizado_em = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("user_id", "order_id", name="uq_mlpedido_user_order"),)


class MLPedidoItemCache(Base):
    """Cache por item de pedido — agregação rápida de vendas por item_id/SKU e janela."""

    __tablename__ = "ml_pedido_item_cache"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    order_id = Column(String, nullable=False, index=True)
    item_id = Column(String, nullable=True, index=True)
    sku = Column(String, nullable=True, index=True)
    titulo = Column(String, nullable=True)
    quantidade = Column(Integer, default=0)
    unit_price = Column(Float, default=0.0)
    receita = Column(Float, default=0.0)                     # unit_price * quantidade
    sale_fee = Column(Float, default=0.0)
    status = Column(String, nullable=True, index=True)
    date_created = Column(DateTime, nullable=True, index=True)

    __table_args__ = (UniqueConstraint("user_id", "order_id", "item_id", name="uq_mlpeditem_user_order_item"),)


class AgenteConfig(Base):
    """Configuração de automação dos agentes por usuário (modo automático, agentes ligados, teto)."""

    __tablename__ = "agente_config"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    automatico = Column(Boolean, default=False)          # roda sozinho no agendador
    kill_switch = Column(Boolean, default=False)         # trava tudo (nada é aplicado)
    agentes = Column(JSON, nullable=True)                # {"margem": true, "giro": true, ...}
    max_por_execucao = Column(Integer, default=15)       # teto de aplicações por rodada
    teto_desconto_pct = Column(Integer, nullable=True)   # desconto máximo que a automação aplica (None = sem teto)
    intervalo_horas = Column(Integer, default=6)         # de quantas em quantas horas
    ultima_execucao_auto = Column(DateTime, nullable=True)
    atualizado_em = Column(DateTime, default=datetime.utcnow)


class AgenteExecucao(Base):
    """Log de cada rodada dos agentes (manual ou automática)."""

    __tablename__ = "agente_execucao"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    quando = Column(DateTime, default=datetime.utcnow, index=True)
    gatilho = Column(String, nullable=True)              # manual | auto
    aplicados = Column(Integer, default=0)
    ignorados = Column(Integer, default=0)
    falhas = Column(Integer, default=0)
    detalhe = Column(JSON, nullable=True)                # [{item_id, titulo, agente, desconto_pct, preco, status}]


# ─────────────────────────────────────────────────────────────────────────────
# PROVA DE EXPEDIÇÃO — Mesa de Separação (mockup v3 aprovado)
# Cada pedido conferido gera um DOSSIÊ: sessão + takes (imagens) + linha do tempo,
# com cadeia de hash (SHA-256 encadeado). Imagens ficam em tabela PRÓPRIA para não
# pesar nas consultas do painel.
# ─────────────────────────────────────────────────────────────────────────────
class SepSessao(Base):
    """Uma sessão de conferência = um pedido separado = um dossiê."""
    __tablename__ = "sep_sessao"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    codigo = Column(String, nullable=False, index=True)        # SEP-8842
    canal = Column(String, nullable=False)                     # ml | shopee
    pedido_id = Column(String, nullable=False, index=True)     # número do pedido no canal
    cliente = Column(String, nullable=True)
    cliente_doc = Column(String, nullable=True)
    cidade = Column(String, nullable=True)
    uf = Column(String, nullable=True)
    nfe_numero = Column(String, nullable=True)
    rastreio = Column(String, nullable=True)
    valor = Column(Float, nullable=True)
    itens = Column(JSON, nullable=True)        # [{sku,nome,qtd,ean,bin,ncm,peso,imagem}]
    bancada = Column(String, nullable=True)
    operador = Column(String, nullable=True)
    qualidade = Column(String, default="padrao")   # economica | padrao | alta
    aberta_em = Column(DateTime, default=datetime.utcnow, index=True)
    selada_em = Column(DateTime, nullable=True)
    duracao_seg = Column(Integer, nullable=True)
    estado = Column(String, default="aberta", index=True)   # aberta | selada | cancelada
    hash_final = Column(String, nullable=True)     # último elo da cadeia
    integra = Column(Boolean, default=True)
    bytes_total = Column(Integer, default=0)
    usada_em_disputa = Column(Boolean, default=False)
    exportada_por = Column(String, nullable=True)
    exportada_em = Column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "codigo", name="uq_sep_user_codigo"),)


class SepMidia(Base):
    """Take (imagem) do dossiê. O binário fica AQUI, isolado do resto do sistema."""
    __tablename__ = "sep_midia"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    sessao_id = Column(Integer, ForeignKey("sep_sessao.id"), nullable=False, index=True)
    ordem = Column(Integer, default=0)
    passo = Column(String, nullable=True)      # abertura|bancada|conferencia|embalado|etiqueta|fechamento|avulso
    modo = Column(String, default="auto")      # auto | manual | tique
    gatilho = Column(String, nullable=True)    # texto do que disparou
    mime = Column(String, default="image/jpeg")
    largura = Column(Integer, nullable=True)
    altura = Column(Integer, nullable=True)
    bytes = Column(Integer, default=0)
    dados = Column(LargeBinary, nullable=True)   # o JPEG já com a marca queimada
    sha256 = Column(String, nullable=False, index=True)
    hash_anterior = Column(String, nullable=True)
    hash_elo = Column(String, nullable=False)    # sha256(hash_anterior + sha256 + meta)
    criada_em = Column(DateTime, default=datetime.utcnow, index=True)


class SepEvento(Base):
    """Linha do tempo do dossiê: bipagens, tiques, divergências, selagem."""
    __tablename__ = "sep_evento"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    sessao_id = Column(Integer, ForeignKey("sep_sessao.id"), nullable=False, index=True)
    tipo = Column(String, nullable=False)   # abertura|bipagem_ok|bipagem_erro|tique|take|selagem|export
    descricao = Column(String, nullable=True)
    sku = Column(String, nullable=True)
    dados = Column(JSON, nullable=True)
    midia_id = Column(Integer, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow, index=True)


class ShopeePedidoCache(Base):
    """Cache dos pedidos Shopee. O painel lê DAQUI; a API só é chamada na sincronização
    (delta por update_time) ou quando um pedido específico é atualizado. Isso derruba
    o volume de chamadas de centenas por carga para poucas dezenas por dia."""
    __tablename__ = "shopee_pedido_cache"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    order_sn = Column(String, nullable=False, index=True)
    status = Column(String, nullable=True, index=True)
    update_time = Column(Integer, nullable=True, index=True)   # epoch do canal
    create_time = Column(Integer, nullable=True, index=True)
    ship_by = Column(Integer, nullable=True)
    total = Column(Float, nullable=True)
    comprador = Column(String, nullable=True)
    cliente = Column(String, nullable=True)
    cidade = Column(String, nullable=True)
    uf = Column(String, nullable=True)
    rastreio = Column(String, nullable=True)
    nf_numero = Column(String, nullable=True)
    payload = Column(JSON, nullable=True)      # o pedido enriquecido, pronto para o painel
    desmascarado = Column(Boolean, default=False)
    sincronizado_em = Column(DateTime, default=datetime.utcnow, index=True)
    __table_args__ = (UniqueConstraint("user_id", "order_sn", name="uq_shopee_user_ordersn"),)


class SyncEstado(Base):
    """Marca d'água da última sincronização por canal — base do delta."""
    __tablename__ = "sync_estado"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    canal = Column(String, nullable=False)      # shopee | ml
    ultimo_sync = Column(DateTime, nullable=True)
    ultimo_update_time = Column(Integer, nullable=True)   # maior update_time visto
    pedidos_cache = Column(Integer, default=0)
    chamadas_ultimo_sync = Column(Integer, default=0)
    __table_args__ = (UniqueConstraint("user_id", "canal", name="uq_sync_user_canal"),)


class ShopeeAvaliacaoCache(Base):
    """Cache permanente das avaliações. O que já foi respondido NUNCA é relido:
    a varredura passa a pedir só `comment_status=UNANSWERED` e o resto vem daqui.
    Isso derruba get_comment de ~600/dia para poucas dezenas e elimina as tentativas
    de responder o que já tem resposta (causa provável das 245 falhas de reply)."""
    __tablename__ = "shopee_avaliacao_cache"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    comment_id = Column(String, nullable=False, index=True)
    item_id = Column(String, nullable=True, index=True)
    order_sn = Column(String, nullable=True)
    rating = Column(Integer, nullable=True, index=True)
    comentario = Column(Text, nullable=True)
    comprador = Column(String, nullable=True)
    tem_midia = Column(Boolean, default=False)
    create_time = Column(Integer, nullable=True, index=True)
    respondida = Column(Boolean, default=False, index=True)
    resposta = Column(Text, nullable=True)
    respondida_em = Column(DateTime, nullable=True)
    tentativas = Column(Integer, default=0)        # trava depois de N falhas
    ultimo_erro = Column(String, nullable=True)
    payload = Column(JSON, nullable=True)
    visto_em = Column(DateTime, default=datetime.utcnow, index=True)
    __table_args__ = (UniqueConstraint("user_id", "comment_id", name="uq_aval_user_comment"),)
