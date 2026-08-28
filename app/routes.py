from flask import Blueprint, render_template, request
import os
from app.analysis import calculate_top_gainers

bp = Blueprint('main', __name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'prices.db')

@bp.route('/')
def index():
    try:
        gainers = calculate_top_gainers(DB_PATH, limit=50)
        
        # Apply status filter
        status_filter = request.args.get('status', 'all')
        if status_filter == 'confirmed':
            gainers = gainers[gainers['status'] == 'CONFIRMED']
        elif status_filter == 'unconfirmed':
            gainers = gainers[gainers['status'] == 'UNCONFIRMED']
        
        # Convert to list of dicts for templating
        gainers_list = gainers.to_dict('records') if not gainers.empty else []
        
        return render_template('gainers.html', gainers=gainers_list, current_filter=status_filter)
    
    except Exception as e:
        error_msg = f"Error loading gainers: {str(e)}"
        return render_template('gainers.html', gainers=[], error=error_msg, current_filter='all')
