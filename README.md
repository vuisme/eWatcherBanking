# eWatcherBanking

Dự án này cung cấp một dịch vụ tự động xác nhận giao dịch cho ngân hàng thông qua email, bao gồm cả việc nạp tiền và nhận tiền thông qua email thông báo và tạo mã QR thanh toán.
Hiện tại đang hỗ trợ:
- Timo

## Tính năng

*   **Tự động xác nhận giao dịch nhận tiền:**
    *   Lắng nghe email từ Timo.
    *   Trích xuất thông tin giao dịch từ nội dung email.
    *   Xác nhận giao dịch dựa trên mã giao dịch tạm thời (VCDxxxxxxxxxx).
    *   Gửi request xác nhận đến ứng dụng (hiện tại đang để comment, cần uncomment khi triển khai).
    *   Lưu trữ lịch sử giao dịch.
*   **Tự động xác nhận giao dịch nạp tiền thông qua nội dung chuyển tiền:**
    *   Lắng nghe email từ Timo.
    *   Trích xuất thông tin giao dịch nạp tiền vào số điện thoại (Ví dụ NT0977123456).
    *   Gửi request xác nhận đến ứng dụng (Ở ứng dụng sẽ tự động nạp tiền cho số điện thoại/userID 0977123456).
    *   Lưu trữ lịch sử giao dịch.
*   **Tạo mã QR thanh toán:**
    *   Tạo mã QR động dựa trên số tiền và mã giao dịch.
    *   Tạo mã QR tĩnh dựa trên thông tin tài khoản ngân hàng.
*   **API:**
    *   `/create_transaction`: Tạo mã giao dịch tạm thời và QR code.
    *   `/transaction_history`: Lấy lịch sử giao dịch.
    *   `/qrpay`: Tạo mã QR tĩnh.
    *   `/check_transaction_status`: Kiểm tra trạng thái giao dịch.

## Yêu cầu

*   Python 3.7+
*   Redis
*   Các thư viện trong file `requirements.txt`

## Cài đặt

1.  **Cài đặt Redis:**
    *   Tham khảo hướng dẫn cài đặt Redis trên trang chủ: [https://redis.io/download](https://redis.io/download)
2.  **Clone repository:**
    ```bash
    git clone <repository_url>
    cd <repository_directory>
    ```
3.  **Tạo môi trường ảo (khuyến nghị):**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```
4.  **Cài đặt các thư viện:**
    ```bash
    pip install -r requirements.txt
    ```

## Cấu hình

1.  **Tạo file `.env` trong thư mục gốc của dự án và cấu hình các biến môi trường:**

    ```
    EMAIL_IMAP=imap.gmail.com
    EMAIL_LOGIN=<your_email>
    EMAIL_PASSWORD=<your_email_password>
    CAKE_EMAIL_SENDERS=[địa chỉ email gửi biến động số dư của timo] # Các email của Timo, cách nhau bằng dấu phẩy
    API_KEY=<your_api_key>
    APP_URL=<your_app_url> # Ví dụ: [http://your-app.com/api](http://your-app.com/api)
    REDIS_HOST=localhost
    REDIS_PORT=6379
    REDIS_DB=0
    TRANSACTION_CODE_EXPIRATION=600 # Thời gian hết hạn của mã giao dịch (giây)
    TRANSACTION_HISTORY_KEY=transaction_history
    BANK_CODE=963388 # Mã ngân hàng Timobank
    ACCOUNT_NUMBER=0977091190 # Số tài khoản ngân hàng
    EMAIL_POLL_INTERVAL=20 # Thời gian chờ giữa các lần kiểm tra email (giây)
    ```

    **Lưu ý:**

    *   Thay thế các giá trị `<...>` bằng thông tin của bạn.
    *   `EMAIL_PASSWORD` là mật khẩu ứng dụng (app password) của Gmail, không phải mật khẩu đăng nhập thông thường. Tham khảo cách tạo mật khẩu ứng dụng tại đây: [https://support.google.com/accounts/answer/185833](https://support.google.com/accounts/answer/185833)
    *   Để sử dụng tính năng xác nhận giao dịch, bạn cần uncomment các dòng code `requests.post(...)` trong các hàm `confirm_topup` và `confirm_transaction` và thay thế `APP_URL` bằng URL API của ứng dụng bạn.

## Chạy ứng dụng

```bash
flask run
```

Ứng dụng sẽ chạy trên cổng 5000.

## API Endpoints

### 1. `/create_transaction`

Tạo mã giao dịch tạm thời và QR code để nhận tiền.

**Method:** `POST`

**Headers:**

*   `Authorization`: `Bearer <API_KEY>`

**Request Body:**

```json
{
  "transaction_id": "your_unique_transaction_id",
  "amount": "100000"
}
```

*   `transaction_id`: ID giao dịch duy nhất của bạn.
*   `amount`: Số tiền cần nhận (không bao gồm dấu chấm, phẩy).

**Response (201 Created):**

Trả về ảnh QR code (SVG).

**Response (400 Bad Request):**

```json
{
  "message": "Missing transaction_id or amount"
}
```

**Response (401 Unauthorized):**

```json
{
  "message": "Unauthorized"
}
```

**Response (500 Internal Server Error):**

```json
{
  "message": "Error creating transaction",
  "error": "<error_message>"
}
```

### 2. `/transaction_history`

Lấy lịch sử giao dịch.

**Method:** `GET`

**Headers:**

*   `Authorization`: `Bearer <API_KEY>`

**Response (200 OK):**

```json
[
  {
    "type": "transaction",
    "status": "pending",
    "transaction_id": "your_unique_transaction_id",
    "amount": "100000",
    "description": "Tạo giao dịch VCD1678886400",
    "transaction_time": "2023-03-15T00:00:00+07:00",
    "code": "VCD1678886400"
  },
  {
    "type": "topup",
    "status": "success",
    "phone_number": "NT0977091190",
    "amount": "50000",
    "description": "Giao dịch abcxyz",
    "transaction_time": "2023-03-15T01:00:00+07:00",
    "transaction_type": "increase",
    "response": "confirmed"
  }
]
```

**Response (401 Unauthorized):**

```json
{
  "message": "Unauthorized"
}
```

**Response (404 Not Found):**

```json
{
  "message": "No transactions found"
}
```

**Response (500 Internal Server Error):**

```json
{
  "message": "Error retrieving transaction history",
  "error": "<error_message>"
}
```

### 3. `/qrpay`

Tạo mã QR tĩnh cho tài khoản ngân hàng.

**Method:** `POST`

**Request Body:**

```json
{
  "bank_code": "963388",
  "account_number": "0977091190",
  "purpose": "Ghi chu"
}
```
* `bank_code`: Mã ngân hàng. Mặc định: `963388` (Timo Bank)
* `account_number`: Số tài khoản. Mặc định: `0977091190`
* `purpose`: Nội dung/mục đích. Mặc định: `NT0977091190`

**Response (200 OK):**

Trả về ảnh QR code (SVG).

**Response (400 Bad Request):**

```json
{
  "message": "Missing bank_code or account_number"
}
```

**Response (500 Internal Server Error):**

```json
{
  "message": "Error generating QR code",
  "error": "<error_message>"
}
```

### 4. `/check_transaction_status`

Kiểm tra trạng thái giao dịch dựa trên mã giao dịch (code).

**Method:** `GET`

**Headers:**

*   `Authorization`: `Bearer <API_KEY>`

**Query Parameters:**

*   `code`: Mã giao dịch (ví dụ: `VCD1678886400`).

**Response (200 OK):**

```json
{
  "status": "completed",
  "amount": "100000",
  "timestamp": "1678886400",
  "transaction_id": "your_unique_transaction_id",
  "description": "Giao dịch VCD1678886400",
  "message": "Transaction completed"
}
```

**Response (400 Bad Request):**

```json
{
  "message": "Missing transaction code"
}
```

**Response (401 Unauthorized):**

```json
{
  "message": "Unauthorized"
}
```

**Response (404 Not Found):**

```json
{
  "message": "Transaction not found"
}
```

## Lưu ý

*   Ứng dụng này chỉ xử lý email từ các địa chỉ email được cấu hình trong biến môi trường `CAKE_EMAIL_SENDERS`.
*   Ứng dụng chạy ở chế độ nền và liên tục kiểm tra email mới cũng như kiểm tra các giao dịch hết hạn.

## Phát triển

Nếu bạn muốn đóng góp cho dự án, vui lòng tạo một pull request.

## Giấy phép

MIT License
```
