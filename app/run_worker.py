import logging
from .config import Settings
from .db import Base, make_session_factory
from .messenger import Messenger
from .seed import load_seed
from .worker import run_forever

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
cfg = Settings.load()
engine, Session = make_session_factory(cfg.database_url)
Base.metadata.create_all(engine)
load_seed(Session, cfg.seed_file)
run_forever(Session, cfg, Messenger(cfg))
