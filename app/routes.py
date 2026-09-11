from flask import Blueprint, render_template, request
import os
import json
import sqlite3
from datetime import datetime

from app.analysis import calculate_penny_movers, calculate_early_movers_backtest, get_product_history_info

bp = Blueprint('main', __name__)

GAINERS_JSON_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'top_gainers.json')
LOSERS_JSON_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'top_losers.json')
EARLY_MOVERS_JSON_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'early_movers.json')

@bp.route('/')
def index():
    try:
        # Check if cached JSON exists
        if not os.path.exists(GAINERS_JSON_PATH):
            return render_template('gainers.html', gainers=[], current_filter='all')
        
        # Load from cached JSON
        with open(GAINERS_JSON_PATH, 'r') as f:
            gainers_list = json.load(f)
        
        # Apply status filter
        status_filter = request.args.get('status', 'all')
        if status_filter == 'confirmed':
            gainers_list = [g for g in gainers_list if g['status'] == 'CONFIRMED']
        elif status_filter == 'unconfirmed':
            gainers_list = [g for g in gainers_list if g['status'] == 'UNCONFIRMED']
        
        return render_template('gainers.html', gainers=gainers_list, current_filter=status_filter)
    
    except Exception as e:
        error_msg = f"Error loading gainers: {str(e)}"
        return render_template('gainers.html', gainers=[], error=error_msg, current_filter='all')

@bp.route('/losers')
def losers():
    try:
        # Check if cached JSON exists
        if not os.path.exists(LOSERS_JSON_PATH):
            return render_template('losers.html', losers=[])
        
        # Load from cached JSON
        with open(LOSERS_JSON_PATH, 'r') as f:
            losers_list = json.load(f)
        
        return render_template('losers.html', losers=losers_list)
    
    except Exception as e:
        error_msg = f"Error loading losers: {str(e)}"
        return render_template('losers.html', losers=[], error=error_msg)

@bp.route('/penny-movers')
def penny_movers():
    try:
        movers = calculate_penny_movers('data/prices.db', limit=50)
        movers_list = [] if movers.empty else movers.to_dict('records')
        return render_template('penny_movers.html', movers=movers_list)
    except Exception as e:
        error_msg = f"Error loading penny movers: {str(e)}"
        return render_template('penny_movers.html', movers=[], error=error_msg)

@bp.route('/early-movers')
def early_movers():
    try:
        # Check if cached JSON exists
        if not os.path.exists(EARLY_MOVERS_JSON_PATH):
            return render_template('early_movers.html', movers=[])
        
        # Load from cached JSON
        with open(EARLY_MOVERS_JSON_PATH, 'r') as f:
            movers_list = json.load(f)
        
        return render_template('early_movers.html', movers=movers_list)
    
    except Exception as e:
        error_msg = f"Error loading early movers: {str(e)}"
        return render_template('early_movers.html', movers=[], error=error_msg)

@bp.route('/backtest-early-movers')
def backtest_early_movers():
    try:
        if not os.path.exists('data/signals.db'):
            return render_template('backtest_early_movers.html', signals=[], summary=None)
        
        result = calculate_early_movers_backtest('data/prices.db', 'data/signals.db')
        return render_template(
            'backtest_early_movers.html',
            signals=result['signals'],
            summary=result['summary']
        )
    except Exception as e:
        error_msg = f"Error loading backtest results: {str(e)}"
        return render_template('backtest_early_movers.html', signals=[], summary=None, error=error_msg)

@bp.route('/card/<int:product_id>')
def card_detail(product_id):
    conn = sqlite3.connect('data/prices.db')

    history = conn.execute(
        '''
        SELECT date, market_price
        FROM prices
        WHERE product_id = ?
        AND market_price IS NOT NULL
        ORDER BY date
        ''',
        (product_id,)
    ).fetchall()

    card_info = conn.execute(
    '''
    SELECT card_name, set_name
    FROM prices
    WHERE product_id = ?
    LIMIT 1
    ''',
    (product_id,)
).fetchone()

    conn.close()

    history_info = get_product_history_info('data/prices.db', product_id)

    first_seen_date = history_info['first_seen_date']
    if first_seen_date:
        first_seen_date = datetime.strptime(first_seen_date, '%Y-%m-%d').strftime('%b %-d, %Y')

    return render_template(
    'card_detail.html',
    product_id=product_id,
    history=history,
    dates=[row[0] for row in history],
    prices=[row[1] for row in history],
    card_name=card_info[0],
    set_name=card_info[1],
    first_seen_date=first_seen_date,
    days_of_history=history_info['days_of_history']
)