from flask import Flask, request, render_template, jsonify, send_file
import psycopg2
from io import BytesIO
import barcode
from barcode.writer import ImageWriter
import random
from flask import session, redirect
import csv
import smtplib
import os
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")
BREVO_API_KEY = os.environ.get("BREVO_API_KEY")

configuration = sib_api_v3_sdk.Configuration()
configuration.api_key['api-key'] = BREVO_API_KEY

brevo_api = sib_api_v3_sdk.TransactionalEmailsApi(
    sib_api_v3_sdk.ApiClient(configuration)
)

def get_connection():
    return psycopg2.connect(DATABASE_URL)

# ---------- BARCODE ----------
def generate_barcode(data):
    code = barcode.get('code128', data, writer=ImageWriter())
    buffer = BytesIO()
    code.write(buffer, {"module_height": 8.0, "font_size": 8})
    buffer.seek(0)
    return buffer

@app.route('/barcode/<ulid>')
def barcode_img(ulid):
    return send_file(generate_barcode(ulid), mimetype='image/png')

# ---------- HOME ----------
@app.route('/')
def home():
    return render_template("home.html")

# ---------- DASHBOARD ----------
@app.route('/dashboard', methods=['POST'])
def dashboard():
    hostel = request.form.get("hostel")
    dorms = [f"{hostel}-Dorm-{chr(i)} block" for i in range(65, 85)]
    return render_template("dashboard.html", hostel=hostel, dorms=dorms)

# ---------- SUPERVISOR ----------
@app.route('/get_supervisor', methods=['POST'])
def get_supervisor():
    sup_id = request.form.get("sup_id")
    hostel = request.form.get("hostel")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT supervisor_name, hostel
        FROM employee
        WHERE supervisor_id = %s
    """, (sup_id,))

    row = cur.fetchone()
    conn.close()

    if row and str(row[1]).strip().upper() == str(hostel).strip().upper():
        return jsonify({"name": row[0]})

    return jsonify({"name": ""})

# ---------- CHECKIN ----------
@app.route('/checkin', methods=['POST'])
def checkin():
    return render_template(
        "checkin.html",
        hostel=request.form.get("hostel"),
        supervisor_name=request.form.get("supervisor_name"),
        dorm=request.form.get("dorm")
    )

# ---------- CHECKOUT ----------
@app.route('/checkout', methods=['POST'])
def checkout_page():
    return render_template(
        "checkout.html",
        hostel=request.form.get("hostel"),
        supervisor_name=request.form.get("supervisor_name"),
        dorm=request.form.get("dorm")
    )

# ---------- STUDENT ----------
@app.route('/get_student', methods=['POST'])
def get_student():
    reg = request.form.get("reg")

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT regno, student_name, mobile_number, email_id,
               hostel, block_26_27, room_number
        FROM students
        WHERE UPPER(regno) = UPPER(%s)
    """, (reg,))

    row = cur.fetchone()
    conn.close()

    if row:
        return jsonify({
            "status":"ok",
            "name":row[1],
            "phone":row[2],
            "email":row[3],
            "room":f"{row[4]}-{row[5]}-{row[6]}",
            "hostel":row[4]
        })

    return jsonify({"status":"fail"})

# ---------- UNIQUE ULID ----------
def unique_num():
    conn = get_connection()
    cur = conn.cursor()

    while True:
        num = random.randint(1000,9999)
        cur.execute("SELECT COUNT(*) FROM luggage WHERE ulid LIKE %s",(f"%-{num}",))
        if cur.fetchone()[0] == 0:
            conn.close()
            return str(num)

# ---------- FINAL CHECKIN ----------
@app.route('/final_checkin', methods=['POST'])
def final_checkin():
    print("🔥 final_checkin route called")
    data = request.json
    print(data)
    reg = data['reg']
    items = data['items']
    dorm = data.get('dorm')
    supervisor = data.get('supervisor_name')

    conn = get_connection()
    # Verify supervisor hostel matches student hostel

    cur = conn.cursor()

    cur.execute("""
    SELECT hostel
    FROM students
    WHERE regno = %s
""", (reg,))
    student = cur.fetchone()

    cur.execute("""
    SELECT hostel
    FROM employee
    WHERE supervisor_name = %s
""", (supervisor,))
    emp = cur.fetchone()

    if not student or not emp:
        conn.close()
        return jsonify({
        "status": "error",
        "message": "Student or Supervisor not found."
    }), 400

    if student[0].strip().upper() != emp[0].strip().upper():
        conn.close()
        return jsonify({
        "status": "error",
        "message": "Access Denied. Cross-Hostel Operations are not allowed"
    }), 403

    for it in items:
        cur.execute("""
            SELECT COUNT(*) FROM luggage
            WHERE regno=%s AND item=%s
        """, (reg, it['name']))

        existing_count = cur.fetchone()[0]
        new_items = it['qty'] - existing_count

        if new_items <= 0:
            continue
        
        # Take only the newly generated ULIDs from the frontend
        new_ulids = it["ulids"][existing_count:]

        for ulid in new_ulids:

            print("===================================")
            print("INSERTING RECORD")
            print("ULID :", ulid)
            print("REG  :", reg)
            print("ITEM :", it["name"])
            print("DORM :", dorm)
            print("SUPERVISOR:",repr(supervisor))

            try:
                cur.execute("""
    INSERT INTO luggage
    (ulid, regno, item, num_bags, slot_id,
     checkin_time, checkin_supervisor,
     status, dorm)
    VALUES(%s,%s,%s,1,'AUTO',
           CURRENT_TIMESTAMP,%s,
           'Stored',%s)
""", (
    ulid,
    reg,
    it["name"],
    supervisor,   # 👈 NEW
    dorm
))
                print("✅ INSERT SUCCESS")

            except Exception as e:
                print("❌ INSERT FAILED")
                print(e)

    conn.commit()
    cur.execute("""
    SELECT checkin_supervisor
    FROM luggage
    WHERE ulid=%s
""", (new_ulids[-1],))
    print("CHECKED FROM DB:", cur.fetchone())
    conn.close()
    try:
        print("CALLING EMAIL FUNCTION")
        send_checkin_email(reg, supervisor, dorm)
        print("EMAIL FUNCTION FINISHED")

    except BaseException as e:

        print("################################")

        print(type(e))

        print(e)

        print("################################")

    return jsonify({"status":"ok"})

def send_checkin_email(reg, supervisor, dorm):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT student_name, email_id FROM students WHERE regno=%s",(reg,))
    stu = cur.fetchone()

    cur.execute("""
        SELECT item, ulid, checkin_time
        FROM luggage
        WHERE regno=%s
    """,(reg,))

    rows = cur.fetchall()
    conn.close()

    if not stu:
        return

    name, email = stu

    table = """
<table border="1" style="border-collapse:collapse;width:100%;">
<tr style="background-color:#1e3a8a;color:white;">
<th>Item</th>
<th>ULID</th>
<th>Check-In Time</th>
</tr>
"""
    for r in rows:
        table += f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td></tr>"
    table += "</table>"

    msg = MIMEMultipart()
    msg['From'] = EMAIL
    msg['To'] = email
    msg['Subject'] = "Luggage Check-In Confirmation"
    body = f"""
<html>

<body style="font-family:Arial,sans-serif;">

<p>
Dear <b>{name} ({reg})</b>,
</p>

<p>
The following items have been deposited in
<b>{dorm}</b>
under your account.
</p>

<br>

{table}

<br>

<p>
<b>Supervisor:</b> Mr./Ms. {supervisor}
</p>

<br>

<p>
Thank you.</p>

<p>
Regards,<br>
<b>VIT Hostel Luggage Deposition System</b>
</p>

</body>

</html>
"""

    msg.attach(MIMEText(body, "html"))
   
    print("EMAIL =", EMAIL)
    print("PASSWORD EXISTS =", PASSWORD is not None)
    print("PASSWORD LENGTH =", len(PASSWORD) if PASSWORD else 0)
    print("TO =", email)
    try:

        sender = {
        "name": "VIT Hostel Luggage Deposition System",
        "email": EMAIL
    }

        receiver = [{
        "email": email,
        "name": name
    }]

        email_message = sib_api_v3_sdk.SendSmtpEmail(
        sender=sender,
        to=receiver,
        subject="Luggage Check-In Confirmation",
        html_content=body
    )

        response = brevo_api.send_transac_email(email_message)

        print("✅ Brevo Email Sent")
        print(response)

    except ApiException as e:

        print("❌ Brevo Error")
        print(e)


def send_checkout_email(reg, supervisor, dorm):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("SELECT student_name, email_id FROM students WHERE regno=%s",(reg,))
    stu = cur.fetchone()

    cur.execute("""
        SELECT item, ulid, checkout_time
        FROM luggage WHERE regno=%s
    """,(reg,))

    rows = cur.fetchall()
    conn.close()

    if not stu:
        return

    name, email = stu

    table = """
<table border="1" style="border-collapse:collapse;width:100%;">
<tr style="background-color:#1e3a8a;color:white;">
<th>Item</th>
<th>ULID</th>
<th>Check-Out Time</th>
</tr>
"""
    for r in rows:
        table += f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td></tr>"
    table += "</table>"

    msg = MIMEMultipart()
    msg['From'] = EMAIL
    msg['To'] = email
    msg['Subject'] = "Luggage Check-Out Confirmation"
    body = f"""
<html>

<body style="font-family:Arial,sans-serif;">

<p>
Dear <b>{name} ({reg})</b>,
</p>

<p>
The following items have been collected from
<b>{dorm}</b>.
</p>

<br>

{table}

<br>

<p>
<b>Supervisor:</b> Mr./Ms. {supervisor}
</p>

<br>

<p>
Thank you.</p>

<p>
Regards,<br>
<b>VIT Hostel Luggage Deposition System</b>
</p>

</body>

</html>
"""

    msg.attach(MIMEText(body, "html"))
    try:

        sender = {
        "name": "VIT Hostel Luggage Deposition System",
        "email": EMAIL
    }

        receiver = [{
        "email": email,
        "name": name
    }]

        email_message = sib_api_v3_sdk.SendSmtpEmail(
        sender=sender,
        to=receiver,
        subject="Luggage Check-Out Confirmation",
        html_content=body
    )

        response = brevo_api.send_transac_email(email_message)

        print("✅ Check-Out Email Sent")
        print(response)

    except ApiException as e:

        print("❌ Brevo Error")
        print(e)

# ---------- FINAL CHECKOUT ----------
@app.route('/final_checkout', methods=['POST'])
def final_checkout():

    data = request.json
    reg = data['reg']
    ulids = data['ulids']
    supervisor = data.get('supervisor_name')
    dorm = data.get('dorm')

    conn = get_connection()
    cur = conn.cursor()

    for ulid in ulids:
        cur.execute("""
    UPDATE luggage
    SET status='Collected',
        checkout_time=CURRENT_TIMESTAMP,
        checkout_supervisor=%s
    WHERE ulid=%s
    AND regno=%s
    AND status='Stored'
""", (
    supervisor,   # 👈 NEW
    ulid,
    reg
))

    conn.commit()

    cur.execute("""
        SELECT COUNT(*) FROM luggage
        WHERE regno=%s AND status='Stored'
    """,(reg,))

    remaining = cur.fetchone()[0]
    conn.close()

    if remaining == 0:
        try:
            send_checkout_email(reg, supervisor, dorm)
        except Exception as e:
            print("Checkout Email Error:", e)

    return jsonify({"status":"done"})

# ---------- FETCH FULL ----------
@app.route('/get_luggage_full', methods=['POST'])
def get_luggage_full():
    reg = request.form.get("reg")
    current_dorm=request.form.get("dorm")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
    SELECT dorm
    FROM luggage
    WHERE regno=%s
      AND status='Stored'
    LIMIT 1
""", (reg,))

    row = cur.fetchone()

    if row:
        actual_dorm = row[0]
        if actual_dorm.strip() != current_dorm.strip():
            conn.close()
            return jsonify({
                "status": "wrong_dorm",
                "correct_dorm": actual_dorm
        })
    cur.execute("""
        SELECT item, ulid, checkin_time, status
        FROM luggage
        WHERE regno = %s
    """, (reg,))

    rows = cur.fetchall()
    conn.close()

    return jsonify([{
        "item":r[0],
        "ulid":r[1],
        "time":str(r[2]),
        "status":r[3]
    } for r in rows])

# ---------- DELETE ----------
@app.route('/delete_luggage', methods=['POST'])
def delete_luggage():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("DELETE FROM luggage WHERE regno=%s AND item=%s",
                (request.json.get("reg"), request.json.get("item")))

    conn.commit()
    conn.close()

    return jsonify({"status":"deleted"})

# ---------- FETCH ----------
@app.route('/get_luggage', methods=['POST'])
def get_luggage():

    reg = request.form.get("reg")
    current_dorm=request.form.get("dorm")

    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
    SELECT dorm
    FROM luggage
    WHERE regno=%s
      AND status='Stored'
    LIMIT 1
""", (reg,))

    row = cur.fetchone()

    if row:
        actual_dorm = row[0]
        if actual_dorm != current_dorm:
            conn.close()

            return jsonify({
            "status": "wrong_dorm",
            "correct_dorm": actual_dorm
        })

    cur.execute("""
        SELECT item, ulid, status
        FROM luggage
        WHERE regno=%s
        ORDER BY item, ulid
    """, (reg,))

    rows = cur.fetchall()

    conn.close()

    luggage = {}

    code_map = {
        "Matress & Pillow": "BED",
        "Bucket & Mug": "BUC",
        "Carton Box": "BOX",
        "Suitcase / Baggage": "BAG"
    }

    for item, ulid, status in rows:

        if item not in luggage:

            luggage[item] = {
                "name": item,
                "code": code_map.get(item, "OTH"),
                "qty": 0,
                "ulids": [],
                "status": status
            }

        luggage[item]["qty"] += 1
        luggage[item]["ulids"].append(ulid)

    return jsonify(list(luggage.values()))

# ================= ADMIN =================

app.secret_key = "vit_secret"

ADMIN_USERS = {
    "VITMH": "MH",
    "VITLH": "LH"
}

def admin_required():
    return 'admin' in session

@app.route('/admin_login', methods=['POST'])
def admin_login():
    user = request.form.get("username")
    pwd = request.form.get("password")

    if user in ADMIN_USERS and pwd == user:
        session['admin'] = user
        session['hostel_type'] = ADMIN_USERS[user]
        return redirect('/admin_dashboard')

    return "Invalid Login"

@app.route('/admin_logout')
def admin_logout():
    session.clear()
    return redirect('/')

@app.route('/admin_dashboard')
def admin_dashboard():
    if not admin_required():
        return redirect('/')
    return render_template("admin.html", hostel=session['hostel_type'])

@app.route('/admin_stats', methods=['POST'])
def admin_stats():

    if not admin_required():
        return jsonify({})

    hostel = session.get("hostel_type")

    conn = get_connection()
    cur = conn.cursor()

    # Total Students
    cur.execute("""
        SELECT COUNT(*)
        FROM students
        WHERE hostel=%s
    """, (hostel,))
    students = cur.fetchone()[0]

    # Total Supervisors
    cur.execute("""
        SELECT COUNT(*)
        FROM employee
        WHERE hostel=%s
    """, (hostel,))
    supervisors = cur.fetchone()[0]

    # Total Luggage
    cur.execute("""
        SELECT COUNT(*)
        FROM luggage L
        JOIN students S ON L.regno=S.regno
        WHERE S.hostel=%s
    """, (hostel,))
    luggage = cur.fetchone()[0]

    # Currently Stored
    cur.execute("""
        SELECT COUNT(*)
        FROM luggage L
        JOIN students S ON L.regno=S.regno
        WHERE S.hostel=%s
        AND L.status='Stored'
    """, (hostel,))
    stored = cur.fetchone()[0]

    # Collected
    cur.execute("""
        SELECT COUNT(*)
        FROM luggage L
        JOIN students S ON L.regno=S.regno
        WHERE S.hostel=%s
        AND L.status='Collected'
    """, (hostel,))
    collected = cur.fetchone()[0]

    conn.close()

    return jsonify({
        "students": students,
        "supervisors": supervisors,
        "luggage": luggage,
        "stored": stored,
        "collected": collected
    })

@app.route('/admin_search', methods=['POST'])
def admin_search():

    if not admin_required():
        return jsonify([])

    query = request.form.get("query", "").strip()
    hostel = session.get("hostel_type")

    conn = get_connection()
    cur = conn.cursor()

    # ---------- Search by ULID ----------
    if "-" in query:

        cur.execute("""
            SELECT
                L.regno,
                S.student_name,
                L.item,
                L.ulid,
                L.dorm,
                L.status,
                L.checkin_supervisor,
                L.checkout_supervisor,
                L.checkin_time,
                L.checkout_time
            FROM luggage L
            JOIN students S ON L.regno=S.regno
            WHERE L.ulid=%s
            AND S.hostel=%s
        """, (query, hostel))

    # ---------- Search by RegNo ----------
    elif query.upper().startswith("23"):

        cur.execute("""
            SELECT
                L.regno,
                S.student_name,
                L.item,
                L.ulid,
                L.dorm,
                L.status,
                L.checkin_supervisor,
                L.checkout_supervisor,
                L.checkin_time,
                L.checkout_time
            FROM luggage L
            JOIN students S ON L.regno=S.regno
            WHERE L.regno=%s
            AND S.hostel=%s
        """, (query.upper(), hostel))

    # ---------- Search by Student Name ----------
    else:

        cur.execute("""
            SELECT
                L.regno,
                S.student_name,
                L.item,
                L.ulid,
                L.dorm,
                L.status,
                L.checkin_supervisor,
                L.checkout_supervisor,
                L.checkin_time,
                L.checkout_time
            FROM luggage L
            JOIN students S ON L.regno=S.regno
            WHERE LOWER(S.student_name)
            LIKE LOWER(%s)
            AND S.hostel=%s
        """, ("%" + query + "%", hostel))

    rows = cur.fetchall()

    conn.close()

    return jsonify(rows)

@app.route('/get_admin_luggage', methods=['POST'])
def get_admin_luggage():

    if not admin_required():
        return jsonify([])

    hostel = session.get('hostel_type')

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT L.regno, S.student_name, L.item, L.ulid,
               L.status, L.checkin_time, L.checkout_time
        FROM luggage L
        JOIN students S ON L.regno = S.regno
        WHERE S.hostel = %s
        ORDER BY L.checkin_time DESC
    """, (hostel,))

    rows = cur.fetchall()
    conn.close()

    return jsonify([{
        "reg":r[0],
        "name":r[1],
        "item":r[2],
        "ulid":r[3],
        "status":r[4],
        "in":str(r[5]),
        "out":str(r[6])
    } for r in rows])

@app.route('/admin_search_reg', methods=['POST'])
def admin_search_reg():

    if not admin_required():
        return jsonify([])

    reg = request.form.get("reg")
    hostel = session.get('hostel_type')

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT L.item, L.ulid, L.status
        FROM luggage L
        JOIN students S ON L.regno = S.regno
        WHERE L.regno = %s AND S.hostel = %s
    """, (reg, hostel))

    rows = cur.fetchall()
    conn.close()

    return jsonify(rows)

@app.route('/admin_search_ulid', methods=['POST'])
def admin_search_ulid():

    if not admin_required():
        return jsonify({})

    ulid = request.form.get("ulid")
    hostel = session.get('hostel_type')

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT L.regno, S.student_name, L.item, L.status
        FROM luggage L
        JOIN students S ON L.regno = S.regno
        WHERE L.ulid = %s AND S.hostel = %s
    """, (ulid, hostel))

    row = cur.fetchone()
    conn.close()

    return jsonify(row if row else {})

@app.route('/admin_report')
def admin_report():

    if not admin_required():
        return redirect('/')

    hostel = session.get('hostel_type')

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT 
            L.regno,
            L.ulid,
            L.dorm,
            COUNT(*) OVER (PARTITION BY L.regno, L.item),
            TO_CHAR(L.checkin_time, 'DD-MM-YYYY HH24:MI:SS'),
            TO_CHAR(L.checkout_time, 'DD-MM-YYYY HH24:MI:SS')
        FROM luggage L
        JOIN students S ON L.regno = S.regno
        WHERE S.hostel = %s
        ORDER BY L.regno, L.item
    """, (hostel,))

    rows = cur.fetchall()
    conn.close()

    filename = f"{hostel}_report.csv"

    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)

        writer.writerow([
            "REGNO","ULID","LOCATION","COUNT","CHECKIN TIME","CHECKOUT TIME"
        ])

        for r in rows:
            writer.writerow(r)

    return send_file(filename, as_attachment=True)

if __name__ == "__main__":
    app.run(debug=True)