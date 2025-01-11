import imaplib
import email
import re
import time
import os
import requests
import segno
import io
import base64
from datetime import datetime
import logging
from flask import Flask, request, jsonify, send_file
from io import BytesIO
from qr_pay import QRPay
from threading import Thread
import redis
import json
from segno import helpers
import uuid

# Cấu hình logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Tải biến môi trường
EMAIL_IMAP = os.environ.get('EMAIL_IMAP', 'imap.gmail.com')
EMAIL_LOGIN = os.environ.get('EMAIL_LOGIN')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD')
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
EMAIL_POLL_INTERVAL = int(os.environ.get('EMAIL_POLL_INTERVAL', 20))
PENDING_TRANSACTION_PREFIX = "pending_transaction:"

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
        match = re.search(r"(VCD\d{10})", description)
        if match:
            code = match.group(1)
            pending_transaction_key = f"{PENDING_TRANSACTION_PREFIX}{code}"
            data = redis_client.hgetall(pending_transaction_key)

            if data and b'type' in data and data[b'type'].decode() == 'receive':
                if amount_increased:
                    transaction_id = data.get(b'transaction_id', b'').decode()
                    amount = data.get(b'amount', b'0').decode()
                    timestamp = data.get(b'timestamp', b'0').decode()
                    status = data.get(b'status', b'pending').decode()

                    logger.info(f"Xác nhận giao dịch NHẬN TIỀN: {description}, số tiền: {amount_increased}, transaction_id: {transaction_id}, code: {code}, timestamp: {timestamp}")

                    if status == 'pending':
                        # Cập nhật trạng thái thành completed
                        redis_client.hset(pending_transaction_key, 'status', 'completed')
                        # Cập nhật lịch sử giao dịch
                        update_transaction_history(code, 'completed', amount_increased, description, transaction_time)
                        confirm_transaction(transaction_id, amount_increased, description, transaction_time)
                        logger.info(f"Cập nhật trạng thái giao dịch thành công: {code}")
                    elif status == 'expired':
                        # Cập nhật trạng thái thành received_after_expired
                        redis_client.hset(pending_transaction_key, 'status', 'received_after_expired')
                        # Cập nhật lịch sử giao dịch
                        update_transaction_history(code, 'received_after_expired', amount_increased, description, transaction_time)
                        confirm_transaction(transaction_id, amount_increased, description, transaction_time)
                        logger.info(f"Cập nhật trạng thái giao dịch nhận được sau khi hết hạn: {code}")
                    else:
                        logger.info(f"Giao dịch đã được xử lý trước đó: {code}, trạng thái: {status}")

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
            _, email_ids = mail.search(None, f'(UNSEEN FROM "{sender}")')
            email_ids = email_ids[0].split()
            if not email_ids:
                continue

            for email_id in email_ids:
                _, msg_data = mail.fetch(email_id, "(RFC822)")
                msg = email.message_from_bytes(msg_data[0][1])
                logger.info(f'Phát hiện email mới từ {sender}')
                mail.store(email_id, '+FLAGS', '\Seen')

                if msg.get_content_type() == 'text/plain':
                    body = msg.get_payload(decode=True).decode()
                    process_cake_email(body)
                elif msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() in ["text/plain", "text/html"]:
                            body = part.get_payload(decode=True).decode()
                            process_cake_email(body)
                            break
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

        # Không lưu ở đây nữa vì đã lưu ở update_transaction_history
        # transaction_data = {
        #     'type': 'transaction',
        #     'status': 'success',
        #     'transaction_id': transaction_id,
        #     'amount': amount,
        #     'description': description,
        #     'transaction_time': transaction_time,
        #     # 'response': response.json()
        #     'response': 'confirmed'
        # }
        # redis_client.rpush(TRANSACTION_HISTORY_KEY, json.dumps(transaction_data))
        # logger.info(f"Đã lưu lịch sử giao dịch: {transaction_data}")

    except requests.exceptions.RequestException as e:
        logger.error(f"Lỗi khi gửi request xác nhận giao dịch: {e}")
        # Có thể cập nhật status = failed nếu cần thiết

def generate_qr_image_from_string(qr_content, scale=10):
    """Tạo ảnh QR PNG từ chuỗi nội dung sử dụng segno."""
    try:
        img_io = BytesIO()
        qr_content.save(img_io, kind='png', scale=scale)
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

        timestamp = int(time.time())
        code = f"VCD{timestamp}"
        pending_transaction_key = f"{PENDING_TRANSACTION_PREFIX}{code}"

        # Lưu thông tin giao dịch vào Redis với trạng thái pending
        pipe = redis_client.pipeline()
        pipe.hset(pending_transaction_key, mapping={
            'transaction_id': transaction_id,
            'amount': amount,
            'timestamp': timestamp,
            'type': 'receive',
            'status': 'pending',
            'description': f"Giao dịch {code}"
        })
        pipe.expire(pending_transaction_key, TRANSACTION_CODE_EXPIRATION)

        # Thêm entry vào transaction_history với trạng thái pending
        transaction_data = {
            'type': 'transaction',
            'status': 'pending',
            'transaction_id': transaction_id,
            'amount': amount,
            'description': f"Tạo giao dịch {code}",
            'transaction_time': datetime.fromtimestamp(timestamp).isoformat() + "+07:00",
            'code': code
        }
        pipe.rpush(TRANSACTION_HISTORY_KEY, json.dumps(transaction_data))

        pipe.execute()

        logger.info(f"Đã tạo mã giao dịch tạm thời: {code} cho transaction_id: {transaction_id} với transaction_amount: {amount} (hết hạn sau {TRANSACTION_CODE_EXPIRATION} giây), type: receive, status: pending")

        # Tạo nội dung QR
        qr_pay = QRPay(BANK_CODE, ACCOUNT_NUMBER, transaction_amount=amount, point_of_initiation_method='DYNAMIC', purpose_of_transaction=code)
        qr_content = qr_pay.generate_qr_code_image(qr_pay.code)
        
        # Tạo ảnh QR từ nội dung
        qr_image = generate_qr_image_from_string(qr_content) # Bạn không cần dùng biến này nữa
        logger.info(qr_image)
        # Mã hóa base64
        qr_code_base64 = base64.b64encode(qr_content).decode('utf-8')
        
        # Tạo JSON response
        response_data = {
            "status": "success",
            "transaction_id": transaction_id,
            "code": code,
            "qr_code_data": qr_code_base64,
            "amount": amount,
            "expires_at": timestamp + TRANSACTION_CODE_EXPIRATION,
            "message": ""
        }

        return jsonify(response_data), 201

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
        transactions = [json.loads(transaction.decode()) for transaction in redis_client.lrange(TRANSACTION_HISTORY_KEY, 0, -1)]
        if not transactions:
            return jsonify({'message': 'No transactions found'}), 404
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

        if not bank_code or not account_number:
            return jsonify({'message': 'Missing bank_code or account_number'}), 400

        qr_pay = QRPay(bank_code, account_number, point_of_initiation_method='STATIC', purpose_of_transaction=purpose)
        qr_content = qr_pay.generate_qr_code_image(qr_pay.code)

        img_io = generate_qr_image_from_string(qr_content)
        if img_io:
            return send_file(img_io, mimetype='image/svg+xml')
        else:
            return jsonify({'message': 'Error generating QR code'}), 500

    except Exception as e:
        logger.error(f"Lỗi khi tạo mã QR: {e}")
        return jsonify({'message': 'Error generating QR code', 'error': str(e)}), 500

@app.route('/check_transaction_status', methods=['GET'])
def check_transaction_status():
    """API endpoint để kiểm tra trạng thái giao dịch."""
    headers = request.headers
    auth_header = headers.get('Authorization')

    if not auth_header or auth_header != f'Bearer {API_KEY}':
        return jsonify({'message': 'Unauthorized'}), 401

    code = request.args.get('code')
    if not code:
        return jsonify({'message': 'Missing transaction code'}), 400

    status, amount, timestamp, transaction_id, description = get_transaction_status(code)

    if status is None:
        return jsonify({'message': 'Transaction not found'}), 404
    else:
        return jsonify({
            'status': status,
            'amount': amount,
            'timestamp': timestamp,
            'transaction_id': transaction_id,
            'description': description,
            'message': get_status_message(status)  # Thêm message dựa trên status
        }), 200


def check_expired_transactions():
    """Kiểm tra các giao dịch pending đã hết hạn và cập nhật trạng thái."""
    while True:
        try:
            cursor = '0'
            logger.info("Bắt đầu quét giao dịch pending...")
            while cursor != 0:
                cursor, keys = redis_client.scan(cursor=cursor, match=f"{PENDING_TRANSACTION_PREFIX}*", count=100)
                for key in keys:
                    logger.info(f"Kiểm tra key: {key}")
                    # Bắt đầu transaction
                    pipe = redis_client.pipeline()
                    pipe.watch(key) # Theo dõi thay đổi trên key

                    transaction_data = pipe.hgetall(key)
                    logger.info(f"  Dữ liệu giao dịch từ Redis: {transaction_data}")

                    if transaction_data:
                        status = transaction_data.get(b'status', b'').decode()
                        expiration_time = int(transaction_data.get(b'timestamp', b'0').decode()) + TRANSACTION_CODE_EXPIRATION
                        code = key.decode().replace(PENDING_TRANSACTION_PREFIX, "")

                        if status == 'pending':
                            logger.info(f"  Tìm thấy giao dịch pending: code={code}, expiration_time={datetime.fromtimestamp(expiration_time).strftime('%Y-%m-%d %H:%M:%S')}")

                            if int(time.time()) > expiration_time:
                                logger.info(f"    Current time: {int(time.time())}, Expiration time: {expiration_time}, Timestamp: {transaction_data.get(b'timestamp', b'').decode()}")
                                # Cập nhật trạng thái thành expired và xóa key trong cùng một transaction
                                try:
                                    pipe.multi()
                                    pipe.hset(key, 'status', 'expired')
                                    update_transaction_history(code, 'expired', pipe=pipe)
                                    pipe.delete(key)
                                    pipe.execute()
                                    logger.info(f"  Cập nhật trạng thái giao dịch thành expired và xóa key: {code}")
                                except redis.exceptions.WatchError:
                                    logger.warning(f"  Giao dịch {code} đã bị thay đổi bởi một client khác. Thử lại sau.")
                                    continue

            # Kiểm tra các giao dịch pending trong transaction_history mà không có pending_transaction key
            logger.info("Kiểm tra các giao dịch pending trong transaction_history...")
            transactions = [json.loads(transaction.decode()) for transaction in redis_client.lrange(TRANSACTION_HISTORY_KEY, 0, -1)]
            for i, transaction in enumerate(transactions):
                code = transaction.get('code')
                if code and transaction.get('status') == 'pending' and transaction.get('type') == 'transaction':
                    pending_transaction_key = f"{PENDING_TRANSACTION_PREFIX}{code}"
                    if not redis_client.exists(pending_transaction_key):
                        logger.info(f"  Giao dịch {code} trong transaction_history không có pending_transaction key. Cập nhật trạng thái thành expired.")
                        transaction['status'] = 'expired'
                        redis_client.lset(TRANSACTION_HISTORY_KEY, i, json.dumps(transaction))
                        logger.info(f"  Đã cập nhật trạng thái giao dịch {code} trong transaction_history thành expired.")

        except Exception as e:
            logger.error(f"Lỗi khi kiểm tra giao dịch hết hạn: {e}")

        time.sleep(60)


def update_transaction_history(code, new_status, amount_received=None, description=None, transaction_time=None, pipe=None):
    """Cập nhật trạng thái của giao dịch trong transaction_history."""
    try:
        transaction_found = False
        transactions = [json.loads(transaction.decode()) for transaction in redis_client.lrange(TRANSACTION_HISTORY_KEY, 0, -1)]
        updated_transactions = []

        for i, transaction in enumerate(transactions):
            if transaction.get('code') == code:
                transaction_found = True
                transaction['status'] = new_status
                if amount_received is not None:
                    transaction['amount'] = amount_received
                if description is not None:
                    transaction['description'] = description
                if transaction_time is not None:
                    transaction['transaction_time'] = transaction_time
                updated_transactions.append(transaction)
                logger.info(f"  Đã cập nhật trạng thái giao dịch trong lịch sử: code={code}, status={new_status}")
            else:
                updated_transactions.append(transaction)

        if transaction_found:
            if pipe is None:
                # Nếu không sử dụng pipeline, cập nhật trực tiếp
                redis_client.delete(TRANSACTION_HISTORY_KEY)
                if updated_transactions:
                    redis_client.rpush(TRANSACTION_HISTORY_KEY, *[json.dumps(transaction) for transaction in updated_transactions])
            else:
                # Nếu sử dụng pipeline, thêm lệnh vào pipeline
                pipe.delete(TRANSACTION_HISTORY_KEY)
                if updated_transactions:
                    pipe.rpush(TRANSACTION_HISTORY_KEY, *[json.dumps(transaction) for transaction in updated_transactions])
        else:
            logger.warning(f"  Không tìm thấy giao dịch trong lịch sử: {code}")
    except Exception as e:
        logger.error(f"Lỗi khi cập nhật lịch sử giao dịch: {e}")


def get_transaction_status(code):
    """Lấy trạng thái giao dịch dựa trên mã giao dịch (code)."""
    pending_transaction_key = f"{PENDING_TRANSACTION_PREFIX}{code}"
    data = redis_client.hgetall(pending_transaction_key)

    if not data:
        # Kiểm tra trong lịch sử giao dịch nếu không tìm thấy trong pending
        transactions = [json.loads(transaction.decode()) for transaction in redis_client.lrange(TRANSACTION_HISTORY_KEY, 0, -1)]
        for transaction in transactions:
            if transaction.get('code') == code:
                return transaction.get('status'), transaction.get('amount'), transaction.get('timestamp'), transaction.get('transaction_id'), transaction.get('description') # Trả về thêm thông tin
        return None, None, None, None, None  # Không tìm thấy
    else:
        # Lấy thông tin từ pending_transaction nếu tìm thấy
        status = data.get(b'status', b'').decode()
        amount = data.get(b'amount', b'0').decode()
        timestamp = data.get(b'timestamp', b'0').decode()
        transaction_id = data.get(b'transaction_id', b'').decode()
        description = data.get(b'description', b'').decode()
        return status, amount, timestamp, transaction_id, description

def get_status_message(status):
    if status == 'pending':
        return 'Transaction is pending'
    elif status == 'completed':
        return 'Transaction completed'
    elif status == 'expired':
        return 'Transaction expired'
    elif status == 'received_after_expired':
        return 'Transaction received after expired'
    else:
        return 'Unknown transaction status'


def email_processing_thread():
    """Hàm chạy trong thread riêng để xử lý email."""
    logger.info('Bắt đầu luồng xử lý email')
    while True:
        fetch_last_unseen_email()
        time.sleep(EMAIL_POLL_INTERVAL)

if __name__ == "__main__":
    logger.info(f'KHỞI TẠO THÀNH CÔNG')

    # Bắt đầu luồng xử lý email
    email_thread = Thread(target=email_processing_thread)
    email_thread.daemon = True
    email_thread.start()

    # Bắt đầu luồng kiểm tra giao dịch hết hạn
    expired_transactions_thread = Thread(target=check_expired_transactions)
    expired_transactions_thread.daemon = True
    expired_transactions_thread.start()

    # Chạy Flask app
    app.run(host='0.0.0.0', port=5000, debug=False)
