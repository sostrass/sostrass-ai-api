from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from .config import settings

# Railway entrega "postgres://" em algumas versões; SQLAlchemy quer "postgresql://".
url = settings.database_url or "sqlite:///./dev.db"
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}

if url.startswith("sqlite"):
    engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
else:
    # Postgres (Railway): conexões ociosas são derrubadas; recycle evita travas em
    # conexão morta. Pool com folga para o threadpool e timeout curto para falhar
    # rápido em vez de pendurar a requisição quando o pool esgota.
    engine = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
        pool_timeout=15,
        pool_recycle=280,
        connect_args=connect_args,
    )
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


def init_db():
    from . import models  # noqa: F401  (garante que os modelos sejam registrados)
    Base.metadata.create_all(bind=engine)


def garantir_colunas_extras():
    """Adiciona colunas novas em tabelas já existentes de forma idempotente (Postgres e SQLite),
    sem depender de uma migration Alembic manual. Seguro rodar a cada boot."""
    from sqlalchemy import inspect, text
    # ── Tabelas que o create_all geral pode ter PULADO ─────────────────────────
    # O create_all do boot roda dentro de try/except: pass. Se UMA tabela falha na
    # criação, o create_all estoura e as SEGUINTES nem chegam a ser criadas — foi o
    # que aconteceu com `shopee_avaliacao_cache` (a varredura de avaliações gravava
    # numa tabela inexistente e explodia). Aqui garantimos as críticas isoladamente:
    # cada uma no seu try, checkfirst=True (não toca no que já existe) e com LOG —
    # nada de engolir erro em silêncio.
    try:
        from . import models as _m
        for _nome in ("ShopeeAvaliacaoCache",):
            _cls = getattr(_m, _nome, None)
            if _cls is not None:
                _cls.__table__.create(bind=engine, checkfirst=True)
        print("[precifica] tabelas extras verificadas · shopee_avaliacao_cache OK", flush=True)
    except Exception as _e:  # noqa: BLE001 — logamos em vez de engolir
        print(f"[precifica] garantir_tabelas_extras FALHOU: {_e}", flush=True)
    insp = inspect(engine)
    alvos = {
        "shopee_boost_config": [
            ("auto_selecao", "BOOLEAN DEFAULT FALSE"),
            ("auto_estrategia", "VARCHAR DEFAULT 'estoque_parado'"),
            ("auto_maximo", "INTEGER DEFAULT 30"),
            ("cond_ativo", "BOOLEAN DEFAULT FALSE"),
            ("cond_gatilho_pct", "FLOAT DEFAULT 0"),
            ("cond_max", "INTEGER DEFAULT 3"),
            ("janelas", "JSON"),
            ("cond_estoque", "BOOLEAN DEFAULT FALSE"),
            ("cond_surto", "BOOLEAN DEFAULT FALSE"),
        ],
        "shopee_boost_item": [
            ("auto", "BOOLEAN DEFAULT FALSE"),
            ("condicional", "BOOLEAN DEFAULT FALSE"),
            ("motivo", "VARCHAR"),
            ("boost_ate", "TIMESTAMP"),
            ("ultimo_boost", "TIMESTAMP"),
            ("impulsos", "INTEGER DEFAULT 0"),
            ("prioridade", "INTEGER DEFAULT 0"),
        ],
        "shopee_boost_log": [
            ("nome", "VARCHAR"),
            ("tipo", "VARCHAR DEFAULT 'auto'"),
            ("fim", "TIMESTAMP"),
            ("vendas_atribuidas", "INTEGER"),
            ("atribuido_em", "TIMESTAMP"),
            ("motivo", "VARCHAR"),
            ("criado_em", "TIMESTAMP"),
        ],
        "shopee_promo_config": [
            ("base_comparacao", "VARCHAR DEFAULT 'dia'"),
            ("dias_analise", "INTEGER DEFAULT 30"),
            ("extras", "TEXT"),
        ],
        "shopee_review_config": [
            ("auto_pausa_seg", "INTEGER DEFAULT 5"),
            ("auto_max_ciclo", "INTEGER DEFAULT 10"),
            ("emoji_intensidade", "VARCHAR DEFAULT 'leve'"),
            ("instrucoes_elogio", "VARCHAR DEFAULT ''"),
            ("instrucoes_morna", "VARCHAR DEFAULT ''"),
            ("instrucoes_critica", "VARCHAR DEFAULT ''"),
            ("frases_casa", "JSON"),
            ("frases_proibidas", "JSON"),
            ("cupom_ativo", "BOOLEAN DEFAULT FALSE"),
            ("cupom_codigo", "VARCHAR DEFAULT ''"),
            ("cupom_quando", "VARCHAR DEFAULT 'vips'"),
        ],
        "shopee_venda_snapshot": [
            ("pedidos_6h", "INTEGER DEFAULT 0"),
            ("bucket", "INTEGER DEFAULT 0"),
        ],
        "webhook_eventos": [
            ("resultado", "JSON"),
            ("recebido_em", "DATETIME"),
        ],
        "nfe_config": [
            ("desconto_plataformas", "JSON"),
        ],
        "produto_cache": [
            ("imagem", "VARCHAR"),
            ("marketplaces", "JSON"),
        ],
        "agente_config": [
            ("teto_desconto_pct", "INTEGER"),
        ],
        "ml_item_cache": [
            ("sub_status", "VARCHAR"),
        ],
    }
    try:
        with engine.begin() as conn:
            for tabela, cols in alvos.items():
                if not insp.has_table(tabela):
                    continue
                existentes = {c["name"] for c in insp.get_columns(tabela)}
                for nome, tipo in cols:
                    if nome not in existentes:
                        conn.execute(text(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}"))
    except Exception:  # noqa: BLE001 — nunca derruba o boot por causa disso
        pass


def run_migrations():
    """Sobe o schema com Alembic, seguro para banco novo OU já existente.

    - Banco novo (sem tabelas)        -> upgrade head (cria tudo).
    - Banco já existente sem Alembic  -> stamp head (marca como atual, NÃO recria nada).
    - Banco já versionado             -> upgrade head (aplica migrations pendentes).
    """
    import os
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # onde está o alembic.ini
    cfg = Config(os.path.join(raiz, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(raiz, "alembic"))

    tabelas = set(inspect(engine).get_table_names())
    if "alembic_version" not in tabelas and "users" in tabelas:
        # Banco anterior ao Alembic: já tem o esquema inicial. Carimba na revisão
        # INICIAL (não em head) e então sobe as migrações seguintes — assim as
        # tabelas novas (ex.: oauth_state) são criadas sem recriar as antigas.
        command.stamp(cfg, "5bbde79adba9")
        command.upgrade(cfg, "head")
    else:
        command.upgrade(cfg, "head")  # banco novo cria tudo; versionado aplica pendentes
