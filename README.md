# Crypto Trading Bot

A web-based cryptocurrency trading bot that automates buying and selling of Bitcoin based on customizable thresholds.

## Features

- User authentication system
- Real-time BTC/USDT price monitoring
- Customizable trading parameters
- Automatic threshold-based trading
- Trade history tracking
- Email notifications
- Responsive web interface

## Setup Instructions

1. Clone the repository
2. Create a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Initialize the database:
   ```bash
   python
   >>> from app import db
   >>> db.create_all()
   >>> exit()
   ```

5. Run the application:
   ```bash
   python app.py
   ```

6. Access the application at `http://localhost:5000`

## Configuration

1. Sign up for a new account
2. Navigate to Settings
3. Configure your trading parameters:
   - Buy/Sell thresholds
   - Tolerance percentage
   - Auto threshold settings
   - Trading amount
4. Add your exchange API credentials
5. Enable/disable email notifications

## Security Notes

- API keys are stored encrypted in the database
- Never share your API keys
- Use environment variables for sensitive data
- Regularly update your password

## License

MIT License 