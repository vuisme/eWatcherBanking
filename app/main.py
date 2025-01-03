import imaplib
import email
import re
import time
import os
import requests
from datetime import datetime
import logging
from flask import Flask, request, jsonify
from threading import Thread
import redis
import random
import string
import json

# Cấu hình logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Tải biến môi trường
EMAIL_IMAP = os.environ['EMAIL_IMAP']
EMAIL_LOGIN = os.environ['EMAIL_LOGIN']
EMAIL_PASSWORD = os.environ['EMAIL_PASSWORD']
CAKE_EMAIL_SENDERS = os.environ.get('CAKE_EMAIL_SENDERS', '').split(',')
API_KEY = os.environ.get('API_KEY', '')  # API Key cho ứng dụng
APP_URL = os.environ.get('APP_URL', '')  # URL của ứng dụng
REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
REDIS_DB = int(os.environ.get('REDIS_DB', 0))
TRANSACTION_CODE_EXPIRATION = int(os.environ.get('TRANSACTION_CODE_EXPIRATION', 600))  # Thời gian hết hạn của mã giao dịch (giây)
TRANSACTION_HISTORY_KEY = os.environ.get('TRANSACTION_HISTORY_KEY', 'transaction_history')

# Kết nối Redis
try:
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
    redis_client.ping()  # Kiểm tra kết nối
    logger.info(f"Kết nối Redis thành công: {REDIS_HOST}:{REDIS_PORT} (DB: {REDIS_DB})")
except redis.exceptions.ConnectionError as e:
    logger.error(f"Lỗi kết nối Redis: {e}")
    exit(1)  # Thoát chương trình nếu không kết nối được Redis

# Flask App
app = Flask(__name__)


def extract_transaction_details(body):
    """Trích xuất chi tiết giao dịch từ nội dung email."""
    transaction_details = {}
    # Khởi tạo amount_decreased và amount_increased với giá trị mặc định
    transaction_details["amount_increased"] = None
    transaction_details["amount_decreased"] = None
    # Trích xuất số tiền tăng
    amount_increased_match = re.search(r"vừa tăng ([\d,.]+) VND", body)
    if amount_increased_match:
        transaction_details["amount_increased"] = amount_increased_match.group(1).replace('.', '').replace(',', '')

    # Trích xuất số tiền giảm
    amount_decreased_match = re.search(r"vừa giảm ([\d,.]+) VND", body)
    if amount_decreased_match:
        transaction_details["amount_decreased"] = amount_decreased_match.group(1).replace('.', '').replace(',', '')

    # Lấy thời gian giao dịch
    time_match = re.search(r"vào (\d{2}/\d{2}/\d{4} \d{2}:\d{2})", body)
    if time_match:
        transaction_details["time"] = time_match.group(1)

        # Chuyển đổi sang định dạng ISO 8601
        try:
            datetime_object = datetime.strptime(transaction_details["time"], "%d/%m/%Y %H:%M")
            transaction_details["time"] = datetime_object.isoformat() + "+07:00"
        except ValueError:
            logger.info("Lỗi: Định dạng thời gian không hợp lệ.")

    # Trích xuất số dư hiện tại
    current_balance_match = re.search(r"Số dư hiện tại: ([\d,.]+) VND", body)
    if current_balance_match:
        transaction_details["current_balance"] = current_balance_match.group(1).replace('.', '').replace(',', '')

    # Trích xuất mô tả giao dịch
    description_match = re.search(r"Mô tả: (.+)", body)
    if description_match:
        transaction_details["description"] = description_match.group(1).split("</p>")[0]

    return transaction_details


def process_cake_email(body):
    """Xử lý email từ Cake và gửi thông báo tới ứng dụng nếu cần."""
    transaction_details = extract_transaction_details(body)
    logging.info(transaction_details)
    if transaction_details:
        description = transaction_details.get('description', '')
        amount = transaction_details.get('amount_decreased', '0')
        transaction_time = transaction_details.get("time", 'Không rõ')
        amount_increased = transaction_details.get('amount_increased')
        amount_decreased = transaction_details.get('amount_decreased')
        match = re.match(r"^NT(\d{10})$", description)
        logging.info(match)
        # 1. Xác thực giao dịch chuyển tiền với nội dung "NTsố điện thoại"
        if amount_decreased:  # Kiểm tra nếu amount_decreased khác None và khác 0 (nếu bạn khởi tạo là 0)
            logging.info("amount_decreased")
            if match:
                phone_number = match.group(1)
                logger.info(f"Phát hiện giao dịch chuyển tiền đi: NT{phone_number}, số tiền: {amount_decreased}")
                #confirm_topup(phone_number, amount_decreased, description, transaction_time)
        if amount_increased:  # Kiểm tra nếu amount_decreased khác None và khác 0 (nếu bạn khởi tạo là 0)
            logging.info("amount_increased")
            if match:
                phone_number = match.group(1)
                logger.info(f"Phát hiện giao dịch chuyển tiền đến: NT{phone_number}, số tiền: {amount_increased}")
                #confirm_topup(phone_number, amount_decreased, description, transaction_time)
        # 2. Xác thực giao dịch chuyển tiền với mã tạm thời
        transaction_code_data = redis_client.hgetall(description)
        if transaction_code_data:
            transaction_id = transaction_code_data.get(b'transaction_id', b'').decode()
            
            logger.info(f"Xác nhận giao dịch: {description}, số tiền: {amount}")
            confirm_transaction(transaction_id, amount, description, transaction_time)
            redis_client.delete(description) # Xóa mã giao dịch sau khi đã xác nhận thành công

def fetch_last_unseen_email():
    """Lấy nội dung của email chưa đọc cuối cùng từ hộp thư đến"""
    mail = imaplib.IMAP4_SSL(EMAIL_IMAP)
    try:
        mail.login(EMAIL_LOGIN, EMAIL_PASSWORD)
        mail.select("inbox")

        for sender in CAKE_EMAIL_SENDERS:
            _, email_ids = mail.search(None, f'(UNSEEN FROM "{sender}")')
            email_ids = email_ids[0].split()
            if email_ids:
                email_id = email_ids[-1]
                _, msg_data = mail.fetch(email_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                logger.info(f'Phát hiện email mới từ {sender}')

                if msg.is_multipart():
                    for part in msg.walk():
                        content_type = part.get_content_type()
                        if "text/plain" in content_type or "text/html" in content_type:
                            body = part.get_payload(decode=True).decode()
                            process_cake_email(body)
                else:
                    body = msg.get_payload(decode=True).decode()
                    process_cake_email(body)

    except Exception as e:
        message = f"Lỗi khi xử lý email: {e}"
        logger.error(message)
    finally:
        mail.logout()


def confirm_topup(phone_number, amount, description, transaction_time):
    """Gửi request xác nhận nạp tiền đến ứng dụng và lưu lịch sử giao dịch."""
    headers = {'Authorization': f'Bearer {API_KEY}'}
    payload = {
        'phone_number': phone_number,
        'amount': amount,
        'description': description,
        'transaction_time': transaction_time
    }
    try:
        response = requests.post(f"{APP_URL}/confirm_topup", json=payload, headers=headers)
        response.raise_for_status()
        logger.info(f"Đã gửi request xác nhận nạp tiền cho số điện thoại {phone_number}, số tiền {amount}")

        # Lưu lịch sử giao dịch vào Redis
        transaction_data = {
            'type': 'topup',
            'status': 'success',
            'phone_number': phone_number,
            'amount': amount,
            'description': description,
            'transaction_time': transaction_time,
            'response': response.json()
        }
        redis_client.rpush(TRANSACTION_HISTORY_KEY, json.dumps(transaction_data))
        logger.info(f"Đã lưu lịch sử giao dịch nạp tiền: {transaction_data}")


    except requests.exceptions.RequestException as e:
        logger.error(f"Lỗi khi gửi request xác nhận nạp tiền: {e}")
        # Lưu lịch sử giao dịch vào Redis với trạng thái lỗi
        transaction_data = {
            'type': 'topup',
            'status': 'failed',
            'phone_number': phone_number,
            'amount': amount,
            'description': description,
            'transaction_time': transaction_time,
            'error': str(e)
        }
        redis_client.rpush(TRANSACTION_HISTORY_KEY, json.dumps(transaction_data))
        logger.error(f"Đã lưu lịch sử giao dịch nạp tiền (lỗi): {transaction_data}")


def confirm_transaction(transaction_id, amount, description, transaction_time):
    """Gửi request xác nhận giao dịch đến ứng dụng và lưu lịch sử giao dịch."""
    headers = {'Authorization': f'Bearer {API_KEY}'}
    payload = {
        'transaction_id': transaction_id,
        'amount': amount,
        'description': description,
        'transaction_time': transaction_time
    }
    try:
        response = requests.post(f"{APP_URL}/confirm_transaction", json=payload, headers=headers)
        response.raise_for_status()
        logger.info(f"Đã gửi request xác nhận giao dịch cho transaction_id {transaction_id}, số tiền {amount}")

        # Lưu lịch sử giao dịch vào Redis
        transaction_data = {
            'type': 'transaction',
            'status': 'success',
            'transaction_id': transaction_id,
            'amount': amount,
            'description': description,
            'transaction_time': transaction_time,
            'response': response.json()
        }
        redis_client.rpush(TRANSACTION_HISTORY_KEY, json.dumps(transaction_data))
        logger.info(f"Đã lưu lịch sử giao dịch: {transaction_data}")

    except requests.exceptions.RequestException as e:
        logger.error(f"Lỗi khi gửi request xác nhận giao dịch: {e}")
        # Lưu lịch sử giao dịch vào Redis với trạng thái lỗi
        transaction_data = {
            'type': 'transaction',
            'status': 'failed',
            'transaction_id': transaction_id,
            'amount': amount,
            'description': description,
            'transaction_time': transaction_time,
            'error': str(e)
        }
        redis_client.rpush(TRANSACTION_HISTORY_KEY, json.dumps(transaction_data))
        logger.error(f"Đã lưu lịch sử giao dịch (lỗi): {transaction_data}")


@app.route('/create_transaction', methods=['POST'])
def create_transaction():
    """API endpoint để tạo mã giao dịch tạm thời."""
    headers = request.headers
    auth_header = headers.get('Authorization')

    if not auth_header or auth_header != f'Bearer {API_KEY}':
        return jsonify({'message': 'Unauthorized'}), 401

    try:
        data = request.get_json()
        transaction_id = data.get('transaction_id')
        if not transaction_id:
            return jsonify({'message': 'Missing transaction_id'}), 400

        # Tạo mã giao dịch ngẫu nhiên 6 ký tự (chữ và số)
        code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

        # Lưu mã giao dịch vào Redis với thời gian hết hạn
        redis_client.hset(code, mapping={
            'transaction_id': transaction_id,
            'timestamp': time.time()
        })
        redis_client.expire(code, TRANSACTION_CODE_EXPIRATION)

        logger.info(f"Đã tạo mã giao dịch tạm thời: {code} cho transaction_id: {transaction_id} (hết hạn sau {TRANSACTION_CODE_EXPIRATION} giây)")

        return jsonify({'message': 'Transaction created', 'code': code}), 201
    except Exception as e:
        logger.error(f"Lỗi khi tạo mã giao dịch: {e}")
        return jsonify({'message': 'Error creating transaction', 'error': str(e)}), 500

@app.route('/transaction_history', methods=['GET'])
def get_transaction_history():
    """API endpoint để lấy lịch sử giao dịch."""
    headers = request.headers
    auth_header = headers.get('Authorization')

    if not auth_header or auth_header != f'Bearer {API_KEY}':
        return jsonify({'message': 'Unauthorized'}), 401

    try:
        # Lấy danh sách các giao dịch từ Redis
        transactions = [json.loads(transaction.decode()) for transaction in redis_client.lrange(TRANSACTION_HISTORY_KEY, 0, -1)]

        return jsonify(transactions), 200
    except Exception as e:
        logger.error(f"Lỗi khi lấy lịch sử giao dịch: {e}")
        return jsonify({'message': 'Error retrieving transaction history', 'error': str(e)}), 500

def email_processing_thread():
    """Hàm chạy trong thread riêng để xử lý email."""
    logger.info('Bắt đầu luồng xử lý email')
    while True:
        fetch_last_unseen_email()
        time.sleep(20)

if __name__ == "__main__":
    logger.info(f'KHỞI TẠO THÀNH CÔNG')

    # Bắt đầu luồng xử lý email
    email_thread = Thread(target=email_processing_thread)
    email_thread.daemon = True
    email_thread.start()

    # Chạy Flask app
    app.run(host='0.0.0.0', port=5000)
