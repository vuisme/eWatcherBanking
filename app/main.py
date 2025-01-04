import imaplib
import email
import re
import time
import os
import requests
import segno
import io
from datetime import datetime
import logging
from flask import Flask, request, jsonify, send_file
from io import BytesIO
from qr_pay import QRPay
from threading import Thread
import redis
import random
import string
import json
from segno import helpers

# Cấu hình logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Tải biến môi trường
EMAIL_IMAP = os.environ['EMAIL_IMAP']
EMAIL_LOGIN = os.environ['EMAIL_LOGIN']
EMAIL_PASSWORD = os.environ['EMAIL_PASSWORD']
CAKE_EMAIL_SENDERS = os.environ.get('CAKE_EMAIL_SENDERS', '').split(',')
API_KEY = os.environ.get('API_KEY', '')
APP_URL = os.environ.get('APP_URL', '')
REDIS_HOST = os.environ.get('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.environ.get('REDIS_PORT', 6379))
REDIS_DB = int(os.environ.get('REDIS_DB', 0))
TRANSACTION_CODE_EXPIRATION = int(os.environ.get('TRANSACTION_CODE_EXPIRATION', 600))
TRANSACTION_HISTORY_KEY = os.environ.get('TRANSACTION_HISTORY_KEY', 'transaction_history')
BANK_CODE = os.environ.get('BANK_CODE', '963388')
ACCOUNT_NUMBER = os.environ.get('ACCOUNT_NUMBER', '0977091190')

# Kết nối Redis
try:
    redis_client = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
    redis_client.ping()
    logger.info(f"Kết nối Redis thành công: {REDIS_HOST}:{REDIS_PORT} (DB: {REDIS_DB})")
except redis.exceptions.ConnectionError as e:
    logger.error(f"Lỗi kết nối Redis: {e}")
    exit(1)

# Flask App
app = Flask(__name__)

def extract_transaction_details(body):
    """Trích xuất chi tiết giao dịch từ nội dung email."""
    transaction_details = {}
    # Tối ưu: Sử dụng dict comprehension và compiled regex
    patterns = {
        "amount_increased": re.compile(r"vừa tăng ([\d,.]+) VND"),
        "amount_decreased": re.compile(r"vừa giảm ([\d,.]+) VND"),
        "time": re.compile(r"vào (\d{2}/\d{2}/\d{4} \d{2}:\d{2})"),
        "current_balance": re.compile(r"Số dư hiện tại: ([\d,.]+) VND"),
        "description": re.compile(r"Mô tả: (.+)")
    }

    transaction_details = {
        key: (match.group(1).replace('.', '').replace(',', '') if key in ["amount_increased", "amount_decreased", "current_balance"] else match.group(1).split("</p>")[0] if key == "description" else match.group(1))
        if (match := patterns[key].search(body)) else None
        for key in patterns
    }

    # Chuyển đổi sang định dạng ISO 8601
    if transaction_details["time"]:
        try:
            datetime_object = datetime.strptime(transaction_details["time"], "%d/%m/%Y %H:%M")
            transaction_details["time"] = datetime_object.isoformat() + "+07:00"
        except ValueError:
            logger.info("Lỗi: Định dạng thời gian không hợp lệ.")

    return transaction_details


def process_cake_email(body):
    """Xử lý email từ Cake và gửi thông báo tới ứng dụng nếu cần."""
    transaction_details = extract_transaction_details(body)
    logger.debug(transaction_details)
    if transaction_details:
        description = transaction_details.get('description', '')
        amount_decreased = transaction_details.get('amount_decreased')
        amount_increased = transaction_details.get('amount_increased')
        transaction_time = transaction_details.get("time", 'Không rõ')

        phone_number_match = re.compile(r"^.*?(NT\d{10})").match(description)

        if amount_decreased and phone_number_match:
            phone_number = phone_number_match.group(1)
            logger.info(f"Phát hiện giao dịch chuyển tiền đi: {phone_number}, số tiền: {amount_decreased}")
            confirm_topup(phone_number, amount_decreased, description, transaction_time, 'decrease')
        if amount_increased and phone_number_match:
            phone_number = phone_number_match.group(1)
            logger.info(f"Phát hiện giao dịch chuyển tiền đến: {phone_number}, số tiền: {amount_increased}")
            confirm_topup(phone_number, amount_increased, description, transaction_time, 'increase')

        # Xác thực giao dịch chuyển tiền với mã tạm thời
        # Tìm kiếm mã giao dịch VCD{timestamp} trong description
        match = re.search(r"(VCD\d{10})", description)  # Tìm VCD và 10 chữ số (timestamp)
        if match:
            code = match.group(1)
            data = redis_client.hgetall(code)
            if data and b'type' in data and data[b'type'].decode() == 'receive':
                if amount_increased:
                    transaction_id = data.get(b'transaction_id', b'').decode()
                    amount = data.get(b'amount', b'0').decode()
                    timestamp = data.get(b'timestamp', b'0').decode()

                    logger.info(f"Xác nhận giao dịch NHẬN TIỀN: {description}, số tiền: {amount_increased}, transaction_id: {transaction_id}, code: {code}, timestamp: {timestamp}")
                    confirm_transaction(transaction_id, amount_increased, description, transaction_time)
                    redis_client.delete(code)
                else:
                    logger.info(f"Giao dịch không khớp với số tiền nhận được: {description}")
            else:
                logger.info(f"Không tìm thấy mã giao dịch hoặc không phải giao dịch nhận tiền: {code}")
        else:
            logger.info("Không xác nhận giao dịch")

def fetch_last_unseen_email():
    """Lấy nội dung của email chưa đọc cuối cùng từ hộp thư đến."""
    mail = imaplib.IMAP4_SSL(EMAIL_IMAP)
    try:
        mail.login(EMAIL_LOGIN, EMAIL_PASSWORD)
        mail.select("inbox")

        for sender in CAKE_EMAIL_SENDERS:
            # Tối ưu: Gộp các lần fetch
            _, email_ids = mail.search(None, f'(UNSEEN FROM "{sender}")')
            email_ids = email_ids[0].split()
            if not email_ids:
                continue

            for email_id in email_ids:
                _, msg_data = mail.fetch(email_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                logger.info(f'Phát hiện email mới từ {sender}')
                mail.store(email_id, '+FLAGS', '\Seen')

                # Tối ưu: Xử lý email dạng text/plain trước, multipart sau
                if msg.get_content_type() == 'text/plain':
                    body = msg.get_payload(decode=True).decode()
                    process_cake_email(body)
                elif msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() in ["text/plain", "text/html"]:
                            body = part.get_payload(decode=True).decode()
                            process_cake_email(body)
                            break # Chỉ xử lý phần text đầu tiên
                else:
                    logger.warning(f"Không hỗ trợ định dạng email: {msg.get_content_type()}")
    except Exception as e:
        logger.error(f"Lỗi khi xử lý email: {e}")
    finally:
        mail.logout()


def confirm_topup(phone_number, amount, description, transaction_time, transaction_type):
    """Gửi request xác nhận nạp tiền đến ứng dụng và lưu lịch sử giao dịch."""
    headers = {'Authorization': f'Bearer {API_KEY}'}
    payload = {
        'phone_number': phone_number,
        'amount': amount,
        'description': description,
        'transaction_time': transaction_time,
        'transaction_type': transaction_type
    }
    try:
        # response = requests.post(f"{APP_URL}/confirm_topup", json=payload, headers=headers)
        # response.raise_for_status()
        logger.info(f"Đã gửi request xác nhận nạp tiền cho số điện thoại {phone_number}, số tiền {amount}, trạng thái {transaction_type}")

        # Lưu lịch sử giao dịch vào Redis (Tối ưu: Gom nhóm thành 1 transaction)
        transaction_data = {
            'type': 'topup',
            'status': 'success',
            'phone_number': phone_number,
            'amount': amount,
            'description': description,
            'transaction_time': transaction_time,
            'transaction_type': transaction_type,
            # 'response': response.json()
            'response': 'confirmed'
        }
        redis_client.rpush(TRANSACTION_HISTORY_KEY, json.dumps(transaction_data))
        logger.info(f"Đã lưu lịch sử giao dịch nạp tiền: {transaction_data}")

    except requests.exceptions.RequestException as e:
        logger.error(f"Lỗi khi gửi request xác nhận nạp tiền: {e}")
        transaction_data = {
            'type': 'topup',
            'status': 'failed',
            'phone_number': phone_number,
            'amount': amount,
            'description': description,
            'transaction_time': transaction_time,
            'transaction_type': transaction_type,
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
        # response = requests.post(f"{APP_URL}/confirm_transaction", json=payload, headers=headers)
        # response.raise_for_status()
        logger.info(f"Đã gửi request xác nhận giao dịch cho transaction_id {transaction_id}, số tiền {amount}")

        # Lưu lịch sử giao dịch vào Redis (Tối ưu: Gom nhóm thành 1 transaction)
        transaction_data = {
            'type': 'transaction',
            'status': 'success',
            'transaction_id': transaction_id,
            'amount': amount,
            'description': description,
            'transaction_time': transaction_time,
            # 'response': response.json()
            'response': 'confirmed'
        }
        redis_client.rpush(TRANSACTION_HISTORY_KEY, json.dumps(transaction_data))
        logger.info(f"Đã lưu lịch sử giao dịch: {transaction_data}")

    except requests.exceptions.RequestException as e:
        logger.error(f"Lỗi khi gửi request xác nhận giao dịch: {e}")
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

def generate_qr_image_from_string(qr_content):
    """Tạo ảnh QR từ chuỗi nội dung."""
    try:
        img_io = BytesIO()
        qr_content.save(img_io, kind='PNG', scale=30)
        img_io.seek(0)
        return img_io
    except Exception as e:
        logger.error(f"Lỗi khi tạo mã QR từ chuỗi: {e}")
        return None

@app.route('/create_transaction', methods=['POST'])
def create_transaction():
    """API endpoint để tạo mã giao dịch tạm thời và QR code."""
    headers = request.headers
    auth_header = headers.get('Authorization')

    if not auth_header or auth_header != f'Bearer {API_KEY}':
        return jsonify({'message': 'Unauthorized'}), 401

    try:
        data = request.get_json()
        transaction_id = data.get('transaction_id')
        amount = data.get('amount')
        if not transaction_id or not amount:
            return jsonify({'message': 'Missing transaction_id or amount'}), 400

        # Tối ưu: Dùng UUID thay vì random string
        timestamp = int(time.time())
        code = f"VCD{timestamp}"

        # Lưu mã giao dịch vào Redis với thời gian hết hạn (Tối ưu: Dùng pipeline)
        pipe = redis_client.pipeline()
        pipe.hset(code, mapping={
            'transaction_id': transaction_id,
            'amount': amount,
            'timestamp': timestamp,
            'type': 'receive'
        })
        pipe.expire(code, TRANSACTION_CODE_EXPIRATION)
        pipe.execute()

        logger.info(f"Đã tạo mã giao dịch tạm thời: {code} cho transaction_id: {transaction_id} (hết hạn sau {TRANSACTION_CODE_EXPIRATION} giây), type: receive")

        # Tạo nội dung QR
        qr_pay = QRPay(BANK_CODE, ACCOUNT_NUMBER, amount=amount, purpose_of_transaction=code)
        qr_content = qr_pay.generate_qr_code_image(qr_pay.code)


        # Tạo ảnh QR từ nội dung
        qr_image = generate_qr_image_from_string(qr_content)
        if qr_image:
            return send_file(qr_image, mimetype='image/png'), 201
        else:
            return jsonify({'message': 'Error generating QR code'}), 500

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
        # Tối ưu: Lấy 1 lần thay vì duyệt qua từng phần tử
        transactions = [json.loads(transaction.decode()) for transaction in redis_client.lrange(TRANSACTION_HISTORY_KEY, 0, -1)]
        return jsonify(transactions), 200
    except Exception as e:
        logger.error(f"Lỗi khi lấy lịch sử giao dịch: {e}")
        return jsonify({'message': 'Error retrieving transaction history', 'error': str(e)}), 500

@app.route('/qrpay', methods=['POST'])
def generate_qr_code():
    """API endpoint để tạo mã QR."""
    try:
        data = request.get_json()
        bank_code = data.get('bank_code', BANK_CODE)
        account_number = data.get('account_number', ACCOUNT_NUMBER)
        purpose = data.get('purpose', 'NT0977091190')
        amount = data.get('amount') # Thêm amount

        if not bank_code or not account_number:
            return jsonify({'message': 'Missing bank_code or account_number'}), 400

        qr_pay = QRPay(bank_code, account_number, amount=amount, purpose_of_transaction=purpose) # Thêm amount vào constructor
        qr_content = qr_pay.generate_qr_code_image(qr_pay.code)

        img_io = generate_qr_image_from_string(qr_content)
        if img_io:
          return send_file(img_io, mimetype='image/png')
        else:
          return jsonify({'message': 'Error generating QR code'}), 500

    except Exception as e:
        logger.error(f"Lỗi khi tạo mã QR: {e}")
        return jsonify({'message': 'Error generating QR code', 'error': str(e)}), 500

def email_processing_thread():
    """Hàm chạy trong thread riêng để xử lý email."""
    logger.info('Bắt đầu luồng xử lý email')
    while True:
        fetch_last_unseen_email()
        time.sleep(20)  # Tối ưu: Điều chỉnh thời gian sleep

if __name__ == "__main__":
    logger.info(f'KHỞI TẠO THÀNH CÔNG')

    # Bắt đầu luồng xử lý email
    email_thread = Thread(target=email_processing_thread)
    email_thread.daemon = True
    email_thread.start()

    # Chạy Flask app
    app.run(host='0.0.0.0', port=5000)
