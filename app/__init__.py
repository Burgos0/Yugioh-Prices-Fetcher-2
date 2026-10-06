import os
import sqlite3

from flask import Flask

SITE_NAME = "TCG Trending"


def printing_slug(printing):
    """URL-safe printing name: "1st Edition" -> "1st-edition"."""
    return (printing or "all").strip().lower().replace(" ", "-")


def _last_updated(db_path="data/prices.db"):
    try:
        with sqlite3.connect(db_path) as conn:
            return conn.execute("SELECT MAX(date) FROM printing_prices").fetchone()[0]
    except Exception:
        return None


def create_app():
    app = Flask(__name__)
    app.config['DEBUG'] = False
    # GoatCounter site code (free, cookie-free analytics). Empty = no tracking script.
    app.config['GOATCOUNTER_CODE'] = os.environ.get('GOATCOUNTER_CODE', '').strip()

    app.jinja_env.filters['printing_slug'] = printing_slug

    @app.context_processor
    def site_context():
        return {
            'site_name': SITE_NAME,
            'last_updated': _last_updated(),
            'goatcounter_code': app.config['GOATCOUNTER_CODE'],
        }

    from app.routes import bp
    app.register_blueprint(bp)

    return app
