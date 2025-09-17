from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, g
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
import os
import requests
from datetime import datetime, timedelta
import hmac
import hashlib
import time
from threading import Thread
import schedule
import json
import threading

# Get the absolute path of the current directory
basedir = os.path.abspath(os.path.dirname(__file__))

# Create database directory if it doesn't exist
db_dir = os.path.join(basedir, 'database')
os.makedirs(db_dir, exist_ok=True)

# Create database file path
db_file = os.path.join(db_dir, 'trading_bot.db')

app = Flask(__name__)
# Use SECRET_KEY from env in production; fallback to random for local dev
app.secret_key = os.environ.get('SECRET_KEY', os.urandom(24))

# Prefer DATABASE_URL if provided (e.g., Render PostgreSQL), else SQLite file
database_url = os.environ.get('DATABASE_URL')
app.config['SQLALCHEMY_DATABASE_URI'] = database_url if database_url else f'sqlite:///{db_file}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# Database Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    api_key = db.Column(db.String(200))
    api_secret = db.Column(db.String(200))
    settings = db.relationship('Settings', backref='user', lazy=True, uselist=False)

class Settings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    buy_threshold = db.Column(db.Float, default=0.0)
    sell_threshold = db.Column(db.Float, default=0.0)
    trade_amount = db.Column(db.Float, default=0.0)
    is_trading = db.Column(db.Boolean, default=False)
    demo_mode = db.Column(db.Boolean, default=True)
    demo_btc_balance = db.Column(db.Float, default=1.0)
    demo_usdt_balance = db.Column(db.Float, default=50000.0)
    last_buy_price = db.Column(db.Float, default=0.0)
    price_history = db.Column(db.String, default='[]')  # Store recent price history
    last_check_time = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    sell_all_percentage = db.Column(db.Float, default=0.0)

class TradeHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(10))
    amount = db.Column(db.Float)
    price = db.Column(db.Float)
    profit = db.Column(db.Float, default=0.0)  # Track profit/loss for each trade
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

class PendingBuy(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    price = db.Column(db.Float, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    is_confirmed = db.Column(db.Boolean, default=False)
    is_rejected = db.Column(db.Boolean, default=False)

# Create all database tables
with app.app_context():
    db.create_all()

def fetch_binance_price(symbol: str = 'BTCUSDT') -> float:
    """Safely fetch the latest price from Binance. Returns 0.0 on failure."""
    try:
        response = requests.get(
            'https://api.binance.com/api/v3/ticker/price',
            params={'symbol': symbol},
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        price_str = data.get('price')
        return float(price_str) if price_str is not None else 0.0
    except Exception as error:
        print(f"Error fetching Binance price for {symbol}: {error}")
        return 0.0

def fetch_price_with_fallback() -> float:
    """Fetch BTC/USD price. Order: CoinPaprika → CoinCap (UA) → CoinGecko → Binance."""
    # 1) CoinPaprika
    try:
        r = requests.get('https://api.coinpaprika.com/v1/tickers/btc-bitcoin', timeout=10)
        r.raise_for_status()
        data = r.json()
        usd = data.get('quotes', {}).get('USD', {}).get('price')
        if usd is not None:
            return float(usd)
    except Exception as e:
        print(f"CoinPaprika provider failed: {e}")

    # 2) CoinCap with User-Agent header to avoid sporadic 404s
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get('https://api.coincap.io/v2/assets/bitcoin', headers=headers, timeout=10)
        r.raise_for_status()
        usd = r.json().get('data', {}).get('priceUsd')
        if usd is not None:
            return float(usd)
    except Exception as e:
        print(f"CoinCap provider failed: {e}")

    # 3) CoinGecko
    try:
        r = requests.get(
            'https://api.coingecko.com/api/v3/simple/price',
            params={'ids': 'bitcoin', 'vs_currencies': 'usd'},
            timeout=10
        )
        r.raise_for_status()
        usd = r.json().get('bitcoin', {}).get('usd')
        if usd is not None:
            return float(usd)
    except Exception as e:
        print(f"CoinGecko provider failed: {e}")

    # 4) Binance last (often blocked on serverless)
    price = fetch_binance_price('BTCUSDT')
    if price > 0:
        return price

    return 0.0

def get_binance_signature(data, secret):
    return hmac.new(secret.encode('utf-8'), data.encode('utf-8'), hashlib.sha256).hexdigest()

def get_binance_headers(api_key):
    return {
        'X-MBX-APIKEY': api_key
    }

def get_account_balance(api_key, api_secret):
    try:
        timestamp = int(time.time() * 1000)
        params = f'timestamp={timestamp}'
        signature = get_binance_signature(params, api_secret)
        
        url = f'https://api.binance.com/api/v3/account?{params}&signature={signature}'
        response = requests.get(url, headers=get_binance_headers(api_key))
        
        if response.status_code == 200:
            balances = response.json()['balances']
            btc_balance = next((b for b in balances if b['asset'] == 'BTC'), {'free': '0.0'})
            usdt_balance = next((b for b in balances if b['asset'] == 'USDT'), {'free': '0.0'})
            
            return {
                'btc': float(btc_balance['free']),
                'usdt': float(usdt_balance['free'])
            }
    except Exception as e:
        print(f"Error getting balance: {str(e)}")
    return {'btc': 0.0, 'usdt': 0.0}

def place_order(api_key, api_secret, side, quantity, demo_mode=False, demo_btc_balance=0.0, demo_usdt_balance=0.0, current_price=None):
    print(f"\nAttempting to place {side} order:")
    print(f"Quantity: {quantity:.8f} BTC")
    print(f"Demo mode: {demo_mode}")
    
    if demo_mode:
        if current_price is None:
            try:
                response = requests.get('https://api.binance.com/api/v3/ticker/price', params={'symbol': 'BTCUSDT'})
                current_price = float(response.json()['price'])
            except:
                print("Failed to get current price for demo trade")
                return None

        if side == 'BUY':
            cost = quantity * current_price
            print(f"Buy cost: ${cost:.2f}")
            print(f"Available USDT: ${demo_usdt_balance:.2f}")
            if cost <= demo_usdt_balance:
                print("Demo buy order successful")
                return {
                    'side': 'BUY',
                    'quantity': quantity,
                    'price': current_price,
                    'demo_btc_balance': demo_btc_balance + quantity,
                    'demo_usdt_balance': demo_usdt_balance - cost
                }
            else:
                print("Demo buy order failed - insufficient USDT balance")
        else:  # SELL
            print(f"Available BTC: {demo_btc_balance:.8f}")
            if quantity <= demo_btc_balance:
                proceeds = quantity * current_price
                print(f"Sell proceeds: ${proceeds:.2f}")
                print("Demo sell order successful")
                return {
                    'side': 'SELL',
                    'quantity': quantity,
                    'price': current_price,
                    'demo_btc_balance': demo_btc_balance - quantity,
                    'demo_usdt_balance': demo_usdt_balance + proceeds
                }
            else:
                print("Demo sell order failed - insufficient BTC balance")
        return None

    # Real trading logic
    try:
        timestamp = int(time.time() * 1000)
        params = (
            f'symbol=BTCUSDT&side={side}&type=MARKET&quantity={quantity:.8f}'
            f'&timestamp={timestamp}'
        )
        signature = get_binance_signature(params, api_secret)
        
        url = f'https://api.binance.com/api/v3/order?{params}&signature={signature}'
        print(f"Sending order to Binance...")
        response = requests.post(url, headers=get_binance_headers(api_key))
        
        if response.status_code == 200:
            print("Live order successful")
            return response.json()
        print(f"Live order failed - Status code: {response.status_code}")
        return None
    except Exception as e:
        print(f"Error placing order: {str(e)}")
        return None

def calculate_trade_profit(buy_price, sell_price, amount):
    """Calculate profit/loss for a trade"""
    return (sell_price - buy_price) * amount

def backfill_price_history(user, days=7):
    """Backfill settings.price_history using recent trades or current price."""
    try:
        history = json.loads(user.settings.price_history or "[]")
    except Exception:
        history = []

    if len(history) >= days:
        return

    trades = (
        TradeHistory.query
        .filter_by(user_id=user.id)
        .order_by(TradeHistory.timestamp.desc())
        .limit(days)
        .all()
    )
    trade_prices = [t.price for t in trades if t.price]

    if trade_prices:
        trade_prices = trade_prices[::-1]
        history = (trade_prices + history)[-days:]
    else:
        current_price = fetch_price_with_fallback()
        if current_price:
            history = [current_price] * days

    user.settings.price_history = json.dumps(history)
    db.session.commit()
    print(f"✓ Backfilled {len(history)} prices for {user.email}")

def check_and_execute_trades():
    print("\nStarting continuous trade check loop...")
    while True:  # Make the function run continuously
        with app.app_context():
            print("\n=== Starting Trade Check ===")
            try:
                users = User.query.all()
                current_price = fetch_price_with_fallback()
                print(f"Current BTC price: ${current_price:.2f}")
                
                for user in users:
                    print(f"\nChecking user: {user.email}")
                    print("----------------------------------------")
                    
                    # Check if trading is enabled
                    if not user.settings.is_trading:
                        print("❌ Trading is disabled for this user")
                        print("Please enable trading in settings if you want to trade")
                        continue
                    
                    settings = user.settings
                    print(f"Trading Mode: {'Demo' if settings.demo_mode else 'Live'}")
                    print(f"Trading Status: {'Enabled' if settings.is_trading else 'Disabled'}")
                    print(f"Buy Threshold: ${settings.buy_threshold:.2f}")
                    print(f"Sell Threshold: ${settings.sell_threshold:.2f}")
                    print(f"Trade Amount: {settings.trade_amount:.8f} BTC")
                    print(f"Stop Loss Percentage: {settings.sell_all_percentage:.2f}%")
                    
                    # Update price history
                    try:
                        price_history = json.loads(settings.price_history)
                    except:
                        price_history = []
                    
                    price_history.append(current_price)
                    if len(price_history) > 10:
                        price_history = price_history[-10:]
                    settings.price_history = json.dumps(price_history)
                    
                    # Check API credentials for live trading
                    if not settings.demo_mode:
                        if not user.api_key or not user.api_secret:
                            print("❌ Live trading enabled but no API credentials found")
                            print("Please add your API credentials in settings")
                            continue
                        print("✓ API credentials found for live trading")
                    
                    # Get current balances
                    if settings.demo_mode:
                        btc_balance = settings.demo_btc_balance
                        usdt_balance = settings.demo_usdt_balance
                        print(f"Demo balances - BTC: {btc_balance:.8f}, USDT: ${usdt_balance:.2f}")
                    else:
                        balances = get_account_balance(user.api_key, user.api_secret)
                        btc_balance = balances['btc']
                        usdt_balance = balances['usdt']
                        print(f"Live balances - BTC: {btc_balance:.8f}, USDT: ${usdt_balance:.2f}")

                    # Buy Check
                    print("\n=== BUY CHECK ===")
                    print(f"Current price: ${current_price:.2f}")
                    print(f"Buy threshold: ${settings.buy_threshold:.2f}")
                    print(f"Last buy price: ${settings.last_buy_price:.2f}")
                    
                    # Calculate minimum required price drop (2% from last buy)
                    min_price_drop_percent = 2.0  # 2% minimum drop required
                    if settings.last_buy_price > 0:
                        required_price = settings.last_buy_price * (1 - min_price_drop_percent / 100)
                        print(f"Required 2% drop price: ${required_price:.2f}")
                        print(f"Price dropped enough from last buy: {current_price <= required_price}")
                    
                    # Check if this would be first buy or subsequent buy
                    is_first_buy = settings.last_buy_price == 0
                    
                    # Only buy if price is below threshold AND (it's first buy OR price dropped enough from last buy)
                    can_buy = current_price <= settings.buy_threshold and (
                        is_first_buy or  # First buy
                        current_price <= settings.last_buy_price * (1 - min_price_drop_percent / 100)  # Price dropped enough
                    )
                    
                    if can_buy:
                        print("✓ Buy conditions met!")
                        if settings.last_buy_price > 0:
                            print(f"Price dropped {((settings.last_buy_price - current_price) / settings.last_buy_price * 100):.2f}% from last buy")
                        
                        # Check trade amount
                        if settings.trade_amount <= 0:
                            print("❌ Trade amount is not set or invalid")
                            print(f"Current trade amount: {settings.trade_amount:.8f} BTC")
                            print("Please set a valid trade amount in settings")
                            continue
                        
                        btc_to_buy = settings.trade_amount
                        total_cost = btc_to_buy * current_price
                        
                        # Check if we have enough USDT
                        if total_cost > usdt_balance:
                            print("❌ Cannot buy - insufficient USDT balance")
                            print(f"Need ${total_cost:.2f}, but only have ${usdt_balance:.2f}")
                            continue
                        
                        if is_first_buy:
                            # Execute buy immediately for first buy
                            print("First buy - executing automatically...")
                            order = place_order(
                                user.api_key, 
                                user.api_secret, 
                                'BUY', 
                                btc_to_buy,
                                settings.demo_mode,
                                settings.demo_btc_balance,
                                settings.demo_usdt_balance,
                                current_price
                            )
                            
                            if order:
                                print("✓ Buy order executed successfully!")
                                trade = TradeHistory(
                                    type='buy',
                                    amount=btc_to_buy,
                                    price=current_price,
                                    user_id=user.id
                                )
                                settings.last_buy_price = current_price
                                print(f"Updated last buy price to: ${settings.last_buy_price:.2f}")
                                
                                if settings.demo_mode:
                                    settings.demo_btc_balance += btc_to_buy
                                    settings.demo_usdt_balance -= total_cost
                                    print(f"New demo balances - BTC: {settings.demo_btc_balance:.8f}, USDT: ${settings.demo_usdt_balance:.2f}")

                                db.session.add(trade)
                                db.session.commit()
                            else:
                                print("❌ Buy order failed!")
                        else:
                            # Create pending buy notification for user confirmation
                            print("Subsequent buy - creating notification for user confirmation...")
                            # Check if there's already a pending buy
                            existing_pending = PendingBuy.query.filter_by(
                                user_id=user.id,
                                is_confirmed=False,
                                is_rejected=False
                            ).first()
                            
                            if not existing_pending:
                                pending_buy = PendingBuy(
                                    user_id=user.id,
                                    price=current_price,
                                    amount=btc_to_buy
                                )
                                db.session.add(pending_buy)
                                db.session.commit()
                                print("Created pending buy notification for user confirmation")
                    else:
                        print("ℹ Current price is above buy threshold - waiting for price to drop")
                    
                    # Sell Check
                    if btc_balance > 0:
                        print("\n=== SELL CHECK ===")
                        print(f"Current price: ${current_price:.2f}")
                        print(f"Sell threshold: ${settings.sell_threshold:.2f}")
                        print(f"Last buy price: ${settings.last_buy_price:.2f}")
                        
                        # Calculate and check stop loss
                        stop_loss_price = settings.last_buy_price * (1 - settings.sell_all_percentage / 100)
                        print(f"Stop loss price: ${stop_loss_price:.2f}")
                        print(f"Stop loss percentage: {settings.sell_all_percentage:.2f}%")
                        print(f"Condition check: Current price >= Sell threshold: {current_price >= settings.sell_threshold}")
                        print(f"Condition check: Current price <= Stop loss: {current_price <= stop_loss_price}")
                        
                        if current_price >= settings.sell_threshold or current_price <= stop_loss_price:
                            sell_reason = "Price reached sell threshold" if current_price >= settings.sell_threshold else "Stop loss triggered"
                            print(f"⚠️ {sell_reason}")
                            print(f"Available BTC to sell: {btc_balance:.8f}")
                            
                            print("✓ All sell conditions met, executing sell order...")
                            order = place_order(
                                user.api_key, 
                                user.api_secret, 
                                'SELL', 
                                btc_balance,
                                settings.demo_mode,
                                settings.demo_btc_balance,
                                settings.demo_usdt_balance,
                                current_price
                            )
                            
                            if order:
                                print("✓ Sell order executed successfully!")
                                profit = calculate_trade_profit(settings.last_buy_price, current_price, btc_balance)
                                print(f"Trade profit: ${profit:.2f}")
                                
                                trade = TradeHistory(
                                    type='sell',
                                    amount=btc_balance,
                                    price=current_price,
                                    profit=profit,
                                    user_id=user.id
                                )
                                
                                if settings.demo_mode:
                                    settings.demo_btc_balance = order['demo_btc_balance']
                                    settings.demo_usdt_balance = order['demo_usdt_balance']
                                    print(f"New demo balances - BTC: {settings.demo_btc_balance:.8f}, USDT: ${settings.demo_usdt_balance:.2f}")
                                
                                settings.last_buy_price = 0
                                print("Reset last buy price to 0")
                                
                                db.session.add(trade)
                                db.session.commit()
                            else:
                                print("❌ Sell order failed!")
                        else:
                            print("ℹ Holding position - Current price is between stop loss and sell threshold")
                    else:
                        print("ℹ No BTC balance available for selling")
                    
                    print("----------------------------------------\n")
                
            except Exception as e:
                print(f"❌ Error in trade check: {str(e)}")
                import traceback
                print("Full error traceback:")
                print(traceback.format_exc())
            
            # Sleep for 60 seconds before next check to avoid rate limits
            print("\nWaiting 60 seconds before next check...")
            time.sleep(60)

def start_trading_bot():
    schedule.every(5).seconds.do(check_and_execute_trades)  # Increased frequency to 5 seconds
    while True:
        schedule.run_pending()
        time.sleep(1)

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        
        if User.query.filter_by(email=email).first():
            flash('Email already exists')
            return redirect(url_for('signup'))
        
        hashed_password = generate_password_hash(password)
        new_user = User(email=email, password=hashed_password)
        db.session.add(new_user)
        db.session.commit()
        
        # Create default settings for new user
        default_settings = Settings(user_id=new_user.id)
        db.session.add(default_settings)
        db.session.commit()
        
        flash('Account created successfully')
        return redirect(url_for('login'))
    
    return render_template('signup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']
        
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            session['user_id'] = user.id
            return redirect(url_for('dashboard'))
        
        flash('Invalid credentials')
    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user = User.query.get(session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('login'))
    # Ensure settings exist for this user (prevents template errors)
    if not user.settings:
        default_settings = Settings(user_id=user.id)
        db.session.add(default_settings)
        db.session.commit()
        # Refresh relationship
        user = User.query.get(user.id)
    # Make user available in flask.g for helper fallbacks
    g.user = user
    
    # Get pending buy count
    pending_count = PendingBuy.query.filter_by(
        user_id=user.id,
        is_confirmed=False,
        is_rejected=False
    ).count()
    
    # Get trade history
    trades = TradeHistory.query.filter_by(user_id=user.id).order_by(TradeHistory.timestamp.desc()).all()
    
    # Calculate total profit
    total_profit = sum(trade.profit for trade in trades) if trades else 0
    
    # Calculate 24h profit
    twenty_four_hours_ago = datetime.utcnow() - timedelta(hours=24)
    trades_24h = [trade for trade in trades if trade.timestamp > twenty_four_hours_ago]
    profit_24h = sum(trade.profit for trade in trades_24h) if trades_24h else 0
    
    # Calculate 7-day profit
    seven_days_ago = datetime.utcnow() - timedelta(days=7)
    trades_7d = [trade for trade in trades if trade.timestamp > seven_days_ago]
    profit_7d = sum(trade.profit for trade in trades_7d) if trades_7d else 0
    
    # Calculate win rate
    profitable_trades = len([trade for trade in trades if trade.profit > 0])
    win_rate = (profitable_trades / len(trades) * 100) if trades else 0
    
    # Get recent trades
    recent_trades = trades[:10]  # Last 10 trades
    
    # Backfill price history if empty for immediate stats
    if not user.settings.price_history or user.settings.price_history == "[]":
        backfill_price_history(user, days=7)

    # Calculate price statistics (safe against API failures)
    historical_prices = fetch_historical_data()
    # Local fallback if external failed
    if not historical_prices:
        try:
            stored = json.loads(user.settings.price_history or '[]')
            if isinstance(stored, list) and stored:
                last = stored[-7:]
                historical_prices = [[i, float(v)] for i, v in enumerate(last, start=1) if isinstance(v, (int, float))]
        except Exception:
            pass

    current_price = fetch_price_with_fallback()
    moving_average = calculate_moving_average(historical_prices) if historical_prices else 0
    percentage_change = calculate_percentage_change(current_price, moving_average) if moving_average else 0
    average_low = calculate_average_low(historical_prices) if historical_prices else 0
    average_high = calculate_average_high(historical_prices) if historical_prices else 0
    
    return render_template('dashboard.html',
                         user=user,
                         total_profit=total_profit,
                         profit_24h=profit_24h,
                         profit_7d=profit_7d,
                         win_rate=win_rate,
                         total_trades=len(trades),
                         recent_trades=recent_trades,
                         pending_count=pending_count,
                         moving_average=moving_average if moving_average else 0,
                         percentage_change=percentage_change,
                         average_low=average_low,
                         average_high=average_high)

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user = User.query.get(session['user_id'])
    if request.method == 'POST':
        settings = user.settings
        
        try:
            settings.buy_threshold = float(request.form['buy_threshold'])
            settings.sell_threshold = float(request.form['sell_threshold'])
            settings.trade_amount = float(request.form['trade_amount'])
            settings.sell_all_percentage = float(request.form['sell_all_percentage'])
        except ValueError:
            flash('Invalid input for thresholds or trade amount. Please enter valid numbers.')
            return redirect(url_for('settings'))
        
        settings.is_trading = 'is_trading' in request.form
        settings.demo_mode = 'demo_mode' in request.form
        
        if not settings.demo_mode:
            user.api_key = request.form['api_key']
            user.api_secret = request.form['api_secret']
        
        db.session.commit()
        flash('Settings updated successfully')
        
    return render_template('settings.html', user=user)

@app.route('/trade_history')
def trade_history():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    trades = TradeHistory.query.filter_by(user_id=session['user_id']).order_by(TradeHistory.timestamp.desc()).all()
    return render_template('trade_history.html', trades=trades)

@app.route('/get_balances')
def get_balances():
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401
    
    user = User.query.get(session['user_id'])
    settings = user.settings
    
    if settings.demo_mode:
        return jsonify({
            'btc': settings.demo_btc_balance,
            'usdt': settings.demo_usdt_balance,
            'demo_mode': True
        })
    
    if not user.api_key or not user.api_secret:
        return jsonify({'error': 'API credentials not set'}), 400
    
    balances = get_account_balance(user.api_key, user.api_secret)
    balances['demo_mode'] = False
    return jsonify(balances)

@app.route('/get_btc_price')
def get_btc_price():
    price = fetch_price_with_fallback()
    if price and price > 0:
        return jsonify({'price': float(price)})
    # Still return 200 with a sentinel; client can show "N/A"
    return jsonify({'price': 0.0, 'warning': 'All providers unavailable'}), 200

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect(url_for('login'))

def fetch_historical_data():
    """Fetch last 7 days of BTC/USD daily prices with multiple fallbacks."""

    # 1) Yahoo Finance
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/BTC-USD"
        params = {"interval": "1d", "range": "7d"}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        timestamps = data["chart"]["result"][0]["timestamp"]
        closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        prices = [[ts * 1000, float(c)] for ts, c in zip(timestamps, closes) if c is not None]
        if prices:
            return prices
    except Exception as e:
        print(f"Historical Yahoo Finance failed: {e}")

    # 2) CoinGecko fallback
    try:
        url = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
        params = {"vs_currency": "usd", "days": "7", "interval": "daily"}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        prices = data.get("prices", [])
        if prices:
            return prices
    except Exception as e:
        print(f"Historical CoinGecko failed: {e}")

    # 3) Local settings.price_history fallback
    try:
        if hasattr(g, "user") and g.user and g.user.settings and g.user.settings.price_history:
            local_prices = json.loads(g.user.settings.price_history)
            if local_prices:
                return [[int(time.time() * 1000), float(p)] for p in local_prices[-7:]]
    except Exception as e:
        print(f"Local history fallback failed: {e}")

    return []

def calculate_moving_average(prices, days=7):
    """Return latest moving average using up to the last `days` points.

    Accepts [[ts, price], ...]. If fewer than `days` points are available, it
    averages whatever is available. Returns 0 if the list is empty.
    """
    if not prices:
        return 0
    window = min(len(prices), days)
    tail = prices[-window:]
    return sum(p[1] for p in tail) / window

def calculate_percentage_change(current_price, moving_average):
    if moving_average is None:
        return None
    return ((current_price - moving_average) / moving_average) * 100

def calculate_average_low(prices):
    lows = [price[1] for price in prices]  # Extract the price values
    return min(lows) if lows else 0  # Return the minimum price as the average low

def calculate_average_high(prices):
    highs = [price[1] for price in prices]  # Extract the price values
    return max(highs) if highs else 0  # Return the maximum price as the average high

def check_buy_sell_conditions(current_price, buy_threshold, sell_threshold):
    if current_price <= buy_threshold:
        # Execute buy logic
        print("Buying BTC at:", current_price)
    elif current_price >= sell_threshold:
        # Execute sell logic
        print("Selling BTC at:", current_price)

    print(f"Buy Threshold: {buy_threshold}, Sell Threshold: {sell_threshold}, Current Price: {current_price}")

def fetch_current_btc_price():
    # Fetch the current BTC price with fallbacks
    return fetch_price_with_fallback()

def trading_bot(user_settings):
    while True:
        try:
            current_price = fetch_current_btc_price()
            buy_threshold = user_settings.buy_threshold
            sell_threshold = user_settings.sell_threshold
            
            print(f"\n=== Trading Bot Status ===")
            print(f"Current price: ${current_price:.2f}")
            print(f"Buy threshold: ${buy_threshold:.2f}")
            print(f"Sell threshold: ${sell_threshold:.2f}")
            
            # Get current balances
            if user_settings.demo_mode:
                btc_balance = user_settings.demo_btc_balance
                usdt_balance = user_settings.demo_usdt_balance
                api_key = None
                api_secret = None
            else:
                # Get the user object from the settings
                with db.session.begin():
                    user = User.query.get(user_settings.user_id)
                    api_key = user.api_key
                    api_secret = user.api_secret
                balances = get_account_balance(api_key, api_secret)
                btc_balance = balances['btc']
                usdt_balance = balances['usdt']
            
            # Buy Logic
            if btc_balance == 0 and current_price <= buy_threshold:
                print("\n=== Executing Buy Order ===")
                btc_to_buy = user_settings.trade_amount
                total_cost = btc_to_buy * current_price
                
                if total_cost <= usdt_balance:
                    order = place_order(
                        api_key,
                        api_secret,
                        'BUY',
                        btc_to_buy,
                        user_settings.demo_mode,
                        user_settings.demo_btc_balance,
                        user_settings.demo_usdt_balance,
                        current_price
                    )
                    
                    if order:
                        print("✓ Buy order executed successfully!")
                        # Update balances and create trade record
                        if user_settings.demo_mode:
                            user_settings.demo_btc_balance = order['demo_btc_balance']
                            user_settings.demo_usdt_balance = order['demo_usdt_balance']
                            user_settings.last_buy_price = current_price
                            
                        trade = TradeHistory(
                            type='buy',
                            amount=btc_to_buy,
                            price=current_price,
                            user_id=user_settings.user_id
                        )
                        db.session.add(trade)
                        db.session.commit()
            
            # Sell Logic
            elif btc_balance > 0:
                should_sell = False
                sell_reason = ""
                
                # Check normal sell threshold
                if current_price >= sell_threshold:
                    should_sell = True
                    sell_reason = "Price reached sell threshold"
                
                # Check stop loss
                stop_loss_price = user_settings.last_buy_price * (1 - user_settings.sell_all_percentage / 100)
                if current_price <= stop_loss_price:
                    should_sell = True
                    sell_reason = "Stop loss triggered"
                
                if should_sell:
                    print(f"\n=== Executing Sell Order ({sell_reason}) ===")
                    order = place_order(
                        api_key,
                        api_secret,
                        'SELL',
                        btc_balance,  # Sell all BTC
                        user_settings.demo_mode,
                        user_settings.demo_btc_balance,
                        user_settings.demo_usdt_balance,
                        current_price
                    )
                    
                    if order:
                        print("✓ Sell order executed successfully!")
                        profit = calculate_trade_profit(user_settings.last_buy_price, current_price, btc_balance)
                        
                        if user_settings.demo_mode:
                            user_settings.demo_btc_balance = order['demo_btc_balance']
                            user_settings.demo_usdt_balance = order['demo_usdt_balance']
                            
                        trade = TradeHistory(
                            type='sell',
                            amount=btc_balance,
                            price=current_price,
                            profit=profit,
                            user_id=user_settings.user_id
                        )
                        user_settings.last_buy_price = 0
                        db.session.add(trade)
                        db.session.commit()
            
            time.sleep(60)  # Check every minute
            
        except Exception as e:
            print(f"Error in trading bot: {str(e)}")
            time.sleep(60)  # Wait before retrying

@app.route('/pending_buys')
def pending_buys():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user = User.query.get(session['user_id'])
    pending = PendingBuy.query.filter_by(
        user_id=user.id,
        is_confirmed=False,
        is_rejected=False
    ).all()
    
    return render_template('pending_buys.html', pending=pending)

@app.route('/confirm_buy/<int:buy_id>')
def confirm_buy(buy_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    pending_buy = PendingBuy.query.get_or_404(buy_id)
    if pending_buy.user_id != session['user_id']:
        flash('Unauthorized')
        return redirect(url_for('dashboard'))
    
    # Get current price
    try:
        current_price = float(requests.get('https://api.binance.com/api/v3/ticker/price', 
                                         params={'symbol': 'BTCUSDT'}).json()['price'])
        
        # Calculate price difference
        price_diff = ((current_price - pending_buy.price) / pending_buy.price) * 100
        
        # If price has moved significantly (more than 1%), warn the user
        if abs(price_diff) > 1:
            flash(f'Warning: Price has {"increased" if price_diff > 0 else "decreased"} by {abs(price_diff):.2f}% ' +
                  f'from ${pending_buy.price:.2f} to ${current_price:.2f}')
            return redirect(url_for('pending_buys'))
        
    except Exception as e:
        flash('Error fetching current price. Please try again.')
        return redirect(url_for('pending_buys'))
    
    pending_buy.is_confirmed = True
    db.session.commit()
    
    # Execute the buy order with current price
    user = User.query.get(session['user_id'])
    settings = user.settings
    
    order = place_order(
        user.api_key,
        user.api_secret,
        'BUY',
        pending_buy.amount,
        settings.demo_mode,
        settings.demo_btc_balance,
        settings.demo_usdt_balance,
        current_price  # Using current price instead of old price
    )
    
    if order:
        trade = TradeHistory(
            type='buy',
            amount=pending_buy.amount,
            price=current_price,  # Using current price
            user_id=user.id
        )
        settings.last_buy_price = current_price  # Using current price
        
        if settings.demo_mode:
            settings.demo_btc_balance += pending_buy.amount
            settings.demo_usdt_balance -= (pending_buy.amount * current_price)  # Using current price
        
        db.session.add(trade)
        db.session.commit()
        flash('Buy order executed successfully at current market price')
    else:
        flash('Buy order failed')
    
    return redirect(url_for('dashboard'))

@app.route('/reject_buy/<int:buy_id>')
def reject_buy(buy_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    pending_buy = PendingBuy.query.get_or_404(buy_id)
    if pending_buy.user_id != session['user_id']:
        flash('Unauthorized')
        return redirect(url_for('dashboard'))
    
    pending_buy.is_rejected = True
    db.session.commit()
    flash('Buy order rejected')
    return redirect(url_for('dashboard'))

if __name__ == '__main__':
    # Start the trading bot in a separate thread
    trading_thread = Thread(target=check_and_execute_trades, daemon=True)
    trading_thread.start()
    print("Trading bot started in background thread")
    
    # Run the Flask application
    port = int(os.environ.get('PORT', 5000))
    is_production = os.environ.get('FLASK_ENV') == 'production'
    app.run(host='0.0.0.0', port=port, debug=not is_production)