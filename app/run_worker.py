import logging
from .config import Settings
from .db import create_schema, make_session_factory
from .messenger import Messenger
from .seed import load_seed
from .worker import run_forever

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# httpx INFO includes request URLs containing comment IDs and PSIDs.
logging.getLogger("httpx").setLevel(logging.WARNING)
cfg = Settings.load()
engine, Session = make_session_factory(cfg.database_url)
create_schema(engine)
load_seed(Session, cfg.seed_file)
run_forever(Session, cfg, Messenger(cfg))
