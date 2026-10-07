import os
import sqlite3
from urllib.parse import quote

from flask import Flask

SITE_NAME = "TCG Trending"


def printing_slug(printing):
    """URL-safe printing name: "1st Edition" -> "1st-edition"."""
    return (printing or "all").strip().lower().replace(" ", "-")


def tcgplayer_url(product_id, affiliate_base=""):
    """TCGplayer product page, wrapped in the Impact affiliate link when configured.

    affiliate_base is the tracking link from Impact, e.g.
    https://tcgplayer.pxf.io/c/1234567/1234567/12345 ; the product page is
    passed as the ``u`` (deep link) parameter.
    """
    product_url = f"https://www.tcgplayer.com/product/{int(product_id)}"
    if not affiliate_base:
        return product_url
    sep = "&" if "?" in affiliate_base else "?"
    return f"{affiliate_base}{sep}u={quote(product_url, safe='')}"


def _last_updated(db_path="data/prices.db"):
    try:
        with sqlite3.connect(db_path) as conn:
            return conn.execute("SELECT MAX(date) FROM printing_prices").fetchone()[0]
    except Exception:
        return None


def create_app():
    app = Flask(__name__)
    app.config['DEBUG'] = False
    # Impact affiliate tracking link for TCGplayer. Empty = plain TCGplayer links.
    app.config['AFFILIATE_BASE_URL'] = os.environ.get('AFFILIATE_BASE_URL', '').strip().rstrip('/')
    # Impact site-ownership meta tag, pasted whole. Only a <meta> tag is accepted.
    tag = os.environ.get('IMPACT_VERIFICATION_TAG', '').strip()
    app.config['IMPACT_VERIFICATION_TAG'] = (
        tag if tag.lower().startswith('<meta') and tag.endswith('>') and tag.count('<') == 1 else ''
    )

    app.jinja_env.filters['printing_slug'] = printing_slug
    app.jinja_env.filters['tcgplayer_url'] = (
        lambda product_id: tcgplayer_url(product_id, app.config['AFFILIATE_BASE_URL']))

    @app.context_processor
    def site_context():
        return {
            'site_name': SITE_NAME,
            'last_updated': _last_updated(),
            'impact_verification_tag': app.config['IMPACT_VERIFICATION_TAG'],
        }

    from app.routes import bp
    app.register_blueprint(bp)

    return app
