import csv
import io
import os
import sqlite3
from datetime import date, datetime
from functools import wraps

from flask import Flask, Response, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from face_engine import FaceEngine, FaceEngineError

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY", "development-change-me"),
    DATABASE=os.path.join(app.instance_path, "attendance.db"),
    FACE_DATA=os.path.join(app.instance_path, "faces"),
    MODEL_PATH=os.path.join(app.instance_path, "trainer.yml"),
    LABELS_PATH=os.path.join(app.instance_path, "labels.json"),
)
os.makedirs(app.instance_path, exist_ok=True)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    connection = g.pop("db", None)
    if connection:
        connection.close()


def init_db():
    connection = get_db()
    with app.open_resource("schema.sql") as schema:
        connection.executescript(schema.read().decode())
    if not connection.execute("SELECT id FROM admins LIMIT 1").fetchone():
        connection.execute(
            "INSERT INTO admins(username,password) VALUES(?,?)",
            ("admin", generate_password_hash("admin123")),
        )
    connection.commit()


def engine():
    return FaceEngine(
        app.config["FACE_DATA"], app.config["MODEL_PATH"], app.config["LABELS_PATH"]
    )


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        admin = get_db().execute(
            "SELECT * FROM admins WHERE username=?", (request.form["username"].strip(),)
        ).fetchone()
        if admin and check_password_hash(admin["password"], request.form["password"]):
            session.clear(); session["admin_id"] = admin["id"]
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.get("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))


@app.get("/")
@login_required
def dashboard():
    connection = get_db(); today = date.today().isoformat()
    total = connection.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    present = connection.execute("SELECT COUNT(*) FROM attendance WHERE attendance_date=?", (today,)).fetchone()[0]
    stats = {"students": total, "present": present, "absent": max(total-present, 0),
             "rate": round((present/total*100), 1) if total else 0}
    recent = connection.execute("""
      SELECT a.*,s.student_code,s.name,s.department FROM attendance a
      JOIN students s ON s.id=a.student_id ORDER BY a.id DESC LIMIT 8
    """).fetchall()
    return render_template("dashboard.html", stats=stats, recent=recent)


@app.route("/students/register", methods=["GET", "POST"])
@login_required
def register_student():
    if request.method == "POST":
        payload = request.get_json() or {}
        fields = [payload.get(k, "").strip() for k in ("student_code", "name", "department", "year", "email")]
        frames = payload.get("frames", [])
        if not all(fields[:4]) or len(frames) < 5:
            return jsonify(error="Complete the form and capture at least five face samples."), 400
        connection = get_db()
        try:
            cursor = connection.execute(
                "INSERT INTO students(student_code,name,department,year,email) VALUES(?,?,?,?,?)", fields
            )
            student_id = cursor.lastrowid
            samples = engine().register(student_id, frames)
            engine().train()
            connection.commit()
            return jsonify(success=True, message=f"Student registered with {samples} face samples.")
        except sqlite3.IntegrityError:
            connection.rollback(); return jsonify(error="Student ID already exists."), 409
        except FaceEngineError as exc:
            connection.rollback(); return jsonify(error=str(exc)), 422
    return render_template("register.html")


@app.get("/students")
@login_required
def students():
    rows = get_db().execute("""
      SELECT s.*,COUNT(a.id) attendance_count FROM students s
      LEFT JOIN attendance a ON a.student_id=s.id GROUP BY s.id ORDER BY s.id DESC
    """).fetchall()
    return render_template("students.html", rows=rows)


@app.route("/recognize", methods=["GET", "POST"])
@login_required
def recognize():
    if request.method == "POST":
        frames = (request.get_json() or {}).get("frames", [])
        if len(frames) < 4:
            return jsonify(error="Capture at least four frames for liveness verification."), 400
        try:
            result = engine().recognize(frames)
            student = get_db().execute("SELECT * FROM students WHERE id=?", (result["student_id"],)).fetchone()
            if not student:
                return jsonify(error="Recognized face is not linked to a student."), 404
            today, now = date.today().isoformat(), datetime.now().strftime("%H:%M:%S")
            get_db().execute("""
              INSERT OR IGNORE INTO attendance(student_id,attendance_date,check_in,confidence,liveness_score)
              VALUES(?,?,?,?,?)
            """, (student["id"], today, now, result["confidence"], result["liveness_score"]))
            get_db().commit()
            return jsonify(success=True, student=dict(student), check_in=now, **result)
        except FaceEngineError as exc:
            return jsonify(error=str(exc)), 422
    return render_template("recognize.html")


@app.get("/attendance")
@login_required
def attendance():
    selected = request.args.get("date", date.today().isoformat())
    rows = get_db().execute("""
      SELECT a.*,s.student_code,s.name,s.department,s.year FROM attendance a
      JOIN students s ON s.id=a.student_id WHERE a.attendance_date=? ORDER BY a.check_in DESC
    """, (selected,)).fetchall()
    return render_template("attendance.html", rows=rows, selected=selected)


@app.get("/attendance/export")
@login_required
def export_attendance():
    selected = request.args.get("date", date.today().isoformat())
    rows = get_db().execute("""
      SELECT s.student_code,s.name,s.department,s.year,a.attendance_date,a.check_in,a.confidence,a.liveness_score
      FROM attendance a JOIN students s ON s.id=a.student_id WHERE a.attendance_date=? ORDER BY s.student_code
    """, (selected,)).fetchall()
    stream = io.StringIO(); writer = csv.writer(stream)
    writer.writerow(["Student ID","Name","Department","Year","Date","Check In","Confidence","Liveness Score"])
    writer.writerows([tuple(row) for row in rows])
    return Response(stream.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename=attendance-{selected}.csv"})


@app.cli.command("init-db")
def init_command():
    init_db(); print("Database initialized.")


if __name__ == "__main__":
    with app.app_context(): init_db()
    app.run(debug=True)
