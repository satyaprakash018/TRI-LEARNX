# -------------------------------------------------
# IMPORTS
# -------------------------------------------------
from flask import Flask, render_template, request, redirect, session, flash, send_file
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from bson.objectid import ObjectId
import gridfs
import io
import os
import time
import openai
from dotenv import load_dotenv   # ✅ ADD THIS
from requests_oauthlib import OAuth2Session


# -------------------------------------------------
# LOAD ENV VARIABLES
# -------------------------------------------------
load_dotenv()   # ✅ load .env file

# -------------------------------------------------
# OPENAI CONFIG
# -------------------------------------------------
openai.api_key = os.getenv("OPENAI_API_KEY")

if not openai.api_key:
    raise ValueError("❌ OPENAI_API_KEY not found in .env file")

# -------------------------------------------------
# APP CONFIG
# -------------------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "fallback-secret-key")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB

# -------------------------------------------------
# FILE VALIDATION
# -------------------------------------------------
ALLOWED_EXTENSIONS = {"pdf"}

def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

# -------------------------------------------------
# DATABASE CONNECTION
# -------------------------------------------------
client = MongoClient("mongodb://localhost:27017/")
db = client["study_portal"]

users = db["users"]
materials = db["materials"]
activity_logs = db["activity_logs"]
bookmarks = db["bookmarks"]
notifications = db["notifications"]
fs = gridfs.GridFS(db)

# -----------------------------
# Google OAuth2 Configuration
# Set these in your environment or replace with literal values (not recommended)
# -----------------------------
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_DISCOVERY_URL = ("https://accounts.google.com/.well-known/openid-configuration")
OAUTH_SCOPE = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]
AUTHORIZATION_BASE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


# -------------------------------------------------
# ACTIVITY LOGGER
# -------------------------------------------------
def log_activity(user_id, action):
    activity_logs.insert_one({
        "user_id": user_id,
        "action": action,
        "timestamp": time.time()
    })

# -------------------------------------------------
# LOGIN PAGE
# -------------------------------------------------
@app.route("/")
def index():
    # Redirect root to the premium login    python app.py page so new design shows by default
    return redirect("/login")

# -------------------------------------------------
# REGISTER
# -------------------------------------------------
@app.route("/register")
def register():
    return render_template("register.html")


# -------------------------------------------------
# Google OAuth login (server-side)
# -------------------------------------------------
@app.route("/login_google")
def login_google():
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        flash("Google OAuth credentials not configured.", "danger")
        return redirect(url_for("login"))

    google = OAuth2Session(GOOGLE_CLIENT_ID, scope=OAUTH_SCOPE, redirect_uri=url_for("oauth2callback", _external=True))
    authorization_url, state = google.authorization_url(AUTHORIZATION_BASE_URL, access_type="offline", prompt="consent")

    session["oauth_state"] = state
    return redirect(authorization_url)


@app.route("/oauth2callback")
def oauth2callback():
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        flash("Google OAuth credentials not configured.", "danger")
        return redirect(url_for("login"))

    google = OAuth2Session(GOOGLE_CLIENT_ID, state=session.get("oauth_state"), redirect_uri=url_for("oauth2callback", _external=True))
    try:
        token = google.fetch_token(TOKEN_URL, client_secret=GOOGLE_CLIENT_SECRET, authorization_response=request.url)
    except Exception as e:
        flash("Google authentication failed.", "danger")
        return redirect(url_for("login"))

    # Get user info
    resp = google.get("https://www.googleapis.com/oauth2/v1/userinfo")
    if resp.status_code != 200:
        flash("Failed to fetch user info from Google.", "danger")
        return redirect(url_for("login"))

    info = resp.json()
    email = info.get("email")
    name = info.get("name") or info.get("given_name") or email

    # Create user if not exists
    user = users.find_one({"email": email})
    if not user:
        users.insert_one({
            "name": name,
            "email": email,
            "password": None,
            "college": "",
            "branch": "",
            "year": "",
            "role": "user",
            "oauth_provider": "google"
        })

    # Log in the user
    user = users.find_one({"email": email})
    session["user_id"] = str(user["_id"])
    session["user_name"] = user.get("name", name)
    session["role"] = user.get("role", "user")

    log_activity(session["user_id"], "Logged in with Google")
    flash("Logged in with Google", "success")
    return redirect("/dashboard")

@app.route("/register_user", methods=["POST"])
def register_user():
    email = request.form["email"]

    if users.find_one({"email": email}):
        flash("Email already registered", "danger")
        return redirect("/register")

    users.insert_one({
        "name": request.form["name"],
        "email": email,
        "password": generate_password_hash(request.form["password"]),
        "college": request.form["college"],
        "branch": request.form["branch"],
        "year": request.form["year"],
        "role": "user"
    })

    flash("Registration successful! Please login.", "success")
    add_notification("New user registered 🎉")
    return redirect("/")

# -------------------------------------------------
# LOGIN (GET shows premium login page, POST handles auth)
# -------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html")

    email = request.form.get("email")
    password = request.form.get("password")

    user = users.find_one({"email": email})

    if user and check_password_hash(user["password"], password):
        session["user_id"] = str(user["_id"])
        session["user_name"] = user["name"]
        session["role"] = user.get("role", "user")

        log_activity(session["user_id"], "Logged in")

        return redirect("/dashboard")

    flash("Invalid Email or Password", "danger")
    return redirect("/login")

# -------------------------------------------------
# DASHBOARD
# -------------------------------------------------
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect("/")

    total_question_papers = materials.count_documents({"category": "question_paper"})
    total_study_materials = materials.count_documents({"category": "study_material"})
    total_videos = 3  # Currently 3 hardcoded videos

    recent_materials = list(
        materials.find().sort("_id", -1).limit(5)
    )

    return render_template(
    "dashboard.html",
    name=session["user_name"],
    total_question_papers=total_question_papers,
    total_study_materials=total_study_materials,
    total_videos=total_videos,
    recent_materials=recent_materials,
    notifications=list(notifications.find().sort("timestamp", -1).limit(5)),
    unread_count=notifications.count_documents({"is_read": False})
)


@app.route("/admin/upload")
def admin_upload():
    if session.get("role") != "admin":
        return redirect("/dashboard")

    return render_template("admin_upload.html")

# -------------------------------------------------
# ADMIN UPLOAD PDF (UPDATED WITH AI SUPPORT)
# -------------------------------------------------
import fitz  # PyMuPDF

@app.route("/admin/upload_pdf", methods=["POST"])
def upload_pdf():
    if "user_id" not in session:
        return redirect("/")

    try:
        title = request.form.get("title")
        subject = request.form.get("subject")
        category = request.form.get("category")

        year = request.form.get("year")
        paper_type = request.form.get("paper_type")

        pdf = request.files.get("pdf")

        if not pdf:
            flash("❌ No file uploaded", "danger")
            return redirect("/admin/upload")

        # ✅ Read file
        file_bytes = pdf.read()

        # ✅ Save in GridFS
        file_id = fs.put(file_bytes, filename=pdf.filename)

        # -------------------------------------------------
        # 🤖 AI FEATURE: Extract PDF TEXT
        # -------------------------------------------------
        text_content = ""

        try:
            doc = fitz.open(stream=file_bytes, filetype="pdf")
            for page in doc:
                text_content += page.get_text()
        except:
            text_content = ""

        # -------------------------------------------------
        # SAVE DATA IN DB
        # -------------------------------------------------
        data = {
            "title": title,
            "subject": subject,
            "category": category,
            "file_id": file_id,
            "content": text_content[:20000],  # limit for AI
            "created_at": time.time()
        }

        # 👉 Add extra fields for question paper
        if category == "question_paper":
            data["year"] = year
            data["paper_type"] = paper_type

        materials.insert_one(data)

        flash("✅ PDF uploaded successfully!", "success")
        return redirect("/admin/upload")

    except Exception as e:
        print("UPLOAD ERROR:", e)
        flash("❌ Upload failed", "danger")
        return redirect("/admin/upload")

# -------------------------------------------------
# STUDY MATERIALS (FULL WORKING VERSION)
# -------------------------------------------------
@app.route("/materials")
def materials_page():
    if "user_id" not in session:
        return redirect("/")

    page = int(request.args.get("page", 1))
    per_page = 10
    skip = (page - 1) * per_page

    search_query = request.args.get("q", "")
    selected_subject = request.args.get("subject", "All")

    query = {"category": "study_material"}

    if search_query:
        query["$or"] = [
            {"title": {"$regex": search_query, "$options": "i"}},
            {"subject": {"$regex": search_query, "$options": "i"}}
        ]

    if selected_subject != "All":
        query["subject"] = selected_subject

    total_materials = materials.count_documents(query)

    print("TOTAL:", total_materials)  # 🔍 debug

    all_materials = list(
        materials.find(query)
        .sort("_id", -1)
        .skip(skip)
        .limit(per_page)
    )

    total_pages = (total_materials + per_page - 1) // per_page

    return render_template(
        "materials.html",
        materials=all_materials,
        subjects=materials.distinct("subject", {"category": "study_material"}),
        search_query=search_query,
        selected_subject=selected_subject,
        page=page,
        total_pages=total_pages
    )

# -------------------------------------------------
# ADMIN DELETE MATERIAL
# -------------------------------------------------
@app.route("/admin/delete-material/<material_id>")
def delete_material(material_id):
    if session.get("role") != "admin":
        return redirect("/dashboard")

    material = materials.find_one({"_id": ObjectId(material_id)})

    if material:
        # delete file from GridFS
        fs.delete(material["file_id"])

        # delete record from DB
        materials.delete_one({"_id": ObjectId(material_id)})

        flash("Material deleted successfully", "success")

    return redirect("/materials")
# -------------------------------------------------
# PREVIEW PDF
# -------------------------------------------------

@app.route("/preview/<file_id>")
def preview_pdf(file_id):
    if "user_id" not in session:
        return redirect("/")

    material = materials.find_one({"file_id": ObjectId(file_id)})

    if not material:
        return "PDF not found"

    return render_template(
        "preview.html",
        file_id=file_id,
        title=material["title"],
        subject=material["subject"]
    )

#-------------------------------------------------
# GENERATE SUMMARY (GPT-4O-MINI)
#-------------------------------------------------
@app.route("/generate-summary", methods=["POST"])
def generate_summary():
    if "user_id" not in session:
        return {"summary": "Login required"}

    data = request.json
    file_id = data.get("file_id")

    material = materials.find_one({"file_id": ObjectId(file_id)})

    if not material or "content" not in material:
        return {"summary": "No content found"}

    content = material["content"][:3000]  # limit text

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Summarize this PDF in simple points."},
                {"role": "user", "content": content}
            ]
        )

        summary = response["choices"][0]["message"]["content"]

        return {"summary": summary}

    except Exception as e:
        print("SUMMARY ERROR:", e)
        return {"summary": "Error generating summary"}
    
#-------------------------------------------------
# ASK PDF (GPT-4O-MINI)
#-------------------------------------------------
@app.route("/ask-pdf", methods=["POST"])
def ask_pdf():
    data = request.json
    question = data.get("question")
    file_id = data.get("file_id")

    material = materials.find_one({"file_id": ObjectId(file_id)})

    if not material or "content" not in material:
        return {"answer": "No content found"}

    context = material["content"][:3000]

    try:
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Answer based on given PDF content."},
                {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}
            ]
        )

        answer = response["choices"][0]["message"]["content"]

        return {"answer": answer}

    except:
        return {"answer": "Error generating answer"}  

        
# -------------------------------------------------
# QUESTION PAPERS LIST
# -------------------------------------------------
@app.route("/question-papers")
def question_papers():
    if "user_id" not in session:
        return redirect("/")

    query = {"category": "question_paper"}

    papers = list(
        materials.find(query).sort("_id", -1)
    )

    return render_template(
        "question_papers.html",
        papers=papers
    )



# -------------------------------------------------
# DELETE QUESTION PAPER
# -------------------------------------------------
@app.route("/admin/delete/<id>")
def delete_paper(id):
    if "user_id" not in session or session.get("role") != "admin":
        return redirect("/")

    try:
        paper = materials.find_one({"_id": ObjectId(id)})

        if paper:
            # Delete file from GridFS
            try:
                fs.delete(paper["file_id"])
            except:
                print("File not found in GridFS")

            # Delete DB record
            materials.delete_one({"_id": ObjectId(id)})

            flash("✅ Question paper deleted successfully", "success")
        else:
            flash("❌ Paper not found", "danger")

    except Exception as e:
        print("DELETE ERROR:", e)
        flash("❌ Error deleting paper", "danger")

    return redirect("/question-papers")    

# -------------------------------------------------
# EDIT QUESTION PAPER
# -------------------------------------------------
@app.route("/admin/edit/<id>", methods=["GET", "POST"])
def edit_paper(id):
    if "user_id" not in session or session.get("role") != "admin":
        return redirect("/")

    paper = materials.find_one({"_id": ObjectId(id)})

    if not paper:
        flash("❌ Paper not found", "danger")
        return redirect("/question-papers")

    if request.method == "POST":
        try:
            materials.update_one(
                {"_id": ObjectId(id)},
                {"$set": {
                    "title": request.form.get("title"),
                    "subject": request.form.get("subject"),
                    "year": request.form.get("year"),
                    "paper_type": request.form.get("paper_type")
                }}
            )

            flash("✅ Updated successfully", "success")
            return redirect("/question-papers")

        except Exception as e:
            print("EDIT ERROR:", e)
            flash("❌ Update failed", "danger")

    return render_template("edit_paper.html", paper=paper)    

# -------------------------------------------------
# VIDEOS
# -------------------------------------------------
@app.route("/videos")
def videos():
    if "user_id" not in session:
        return redirect("/")

    log_activity(session["user_id"], "Accessed Videos")
    total_videos = 3  # Currently 3 hardcoded videos
    return render_template("videos.html", total_videos=total_videos)

# -------------------------------------------------
# SERVE PDF + DOWNLOAD COUNT + ACTIVITY
# -------------------------------------------------
@app.route("/material/<file_id>")
def serve_pdf(file_id):
    if "user_id" not in session:
        return redirect("/")

    materials.update_one(
        {"file_id": ObjectId(file_id)},
        {"$inc": {"downloads": 1}}
    )

    log_activity(session["user_id"], "Downloaded a file")

    file = fs.get(ObjectId(file_id))

    return send_file(
        io.BytesIO(file.read()),
        mimetype="application/pdf",
        download_name=file.filename,
        as_attachment=False
    )

# -------------------------------------------------
# PROFILE PAGE
# -------------------------------------------------
@app.route("/profile")
def profile():
    if "user_id" not in session:
        return redirect("/")

    user = users.find_one({"_id": ObjectId(session["user_id"])})
    return render_template("profile.html", user=user)

# -------------------------------------------------
# PROFILE PICTURE UPLOAD
# -------------------------------------------------
@app.route("/upload-profile-pic", methods=["POST"])
def upload_profile_pic():
    if "user_id" not in session:
        return redirect("/")

    file = request.files.get("profile_pic")

    if not file or file.filename == "":
        flash("No file selected", "danger")
        return redirect("/profile")

    filename = secure_filename(file.filename)
    pic_id = fs.put(file, filename=filename)

    users.update_one(
        {"_id": ObjectId(session["user_id"])},
        {"$set": {"profile_pic": pic_id}}
    )

    flash("Profile picture updated", "success")
    return redirect("/profile")

# -------------------------------------------------
# SERVE PROFILE PICTURE
# -------------------------------------------------
@app.route("/profile-pic/<user_id>")
def profile_pic(user_id):
    user = users.find_one({"_id": ObjectId(user_id)})

    if not user or "profile_pic" not in user:
        return redirect("/static/default.png")

    file = fs.get(user["profile_pic"])

    return send_file(
        io.BytesIO(file.read()),
        mimetype="image/jpeg"
    )

# -------------------------------------------------
# UPDATE PROFILE
# -------------------------------------------------
@app.route("/update-profile", methods=["POST"])
def update_profile():
    if "user_id" not in session:
        return redirect("/")

    name = request.form.get("name")
    college = request.form.get("college")
    branch = request.form.get("branch")
    year = request.form.get("year")

    if not name:
        flash("Name cannot be empty", "danger")
        return redirect("/profile")

    users.update_one(
        {"_id": ObjectId(session["user_id"])},
        {"$set": {
            "name": name,
            "college": college,
            "branch": branch,
            "year": year
        }}
    )

    # Update session name also
    session["user_name"] = name

    flash("Profile updated successfully!", "success")
    return redirect("/profile")    


# -------------------------------------------------
# CHANGE PASSWORD
# -------------------------------------------------
@app.route("/change-password", methods=["POST"])
def change_password():
    if "user_id" not in session:
        return redirect("/")

    current_password = request.form.get("current_password")
    new_password = request.form.get("new_password")

    if not current_password or not new_password:
        flash("All fields are required", "danger")
        return redirect("/profile")

    user = users.find_one({"_id": ObjectId(session["user_id"])})

    # Check current password
    if not check_password_hash(user["password"], current_password):
        flash("Current password is incorrect", "danger")
        return redirect("/profile")

    # Optional: Password length check
    if len(new_password) < 6:
        flash("New password must be at least 6 characters", "warning")
        return redirect("/profile")

    # Update password
    hashed_password = generate_password_hash(new_password)

    users.update_one(
        {"_id": ObjectId(session["user_id"])},
        {"$set": {"password": hashed_password}}
    )

    flash("Password updated successfully!", "success")
    return redirect("/profile")


# -------------------------------------------------
# ACTIVITY PAGE
# -------------------------------------------------
@app.route("/activity")
def activity():
    if "user_id" not in session:
        return redirect("/")

    logs = list(
        activity_logs.find(
            {"user_id": session["user_id"]}
        ).sort("timestamp", -1)
    )

    return render_template("activity.html", logs=logs)

# -------------------------------------------------
# DELETE ACCOUNT
# -------------------------------------------------
@app.route("/delete-account")
def delete_account():
    if "user_id" not in session:
        return redirect("/")

    users.delete_one({"_id": ObjectId(session["user_id"])})
    session.clear()

    flash("Account deleted successfully", "success")
    return redirect("/")

# -------------------------------------------------
# ADMIN USER MANAGEMENT
# -------------------------------------------------
@app.route("/admin/users")
def manage_users():
    if session.get("role") != "admin":
        return redirect("/dashboard")

    all_users = list(users.find())
    return render_template("manage_users.html", users=all_users)

@app.route("/admin/delete-user/<user_id>")
def delete_user(user_id):
    if session.get("role") != "admin":
        return redirect("/dashboard")

    users.delete_one({"_id": ObjectId(user_id)})
    flash("User deleted", "success")
    return redirect("/admin/users")


# -------------------------------------------------
# ADMIN ANALYTICS DASHBOARD (FULL VERSION)
# -------------------------------------------------
@app.route("/admin/analytics")
def admin_analytics():
    if session.get("role") != "admin":
        return redirect("/dashboard")

    import datetime

    # Basic Stats
    total_users = users.count_documents({})
    total_materials = materials.count_documents({})

    download_data = list(materials.find({}, {"downloads": 1}))
    total_downloads = sum(item.get("downloads", 0) for item in download_data)

    # Most Downloaded Material
    top_material = materials.find_one(sort=[("downloads", -1)])

    # Most Active User
    pipeline = [
        {"$group": {"_id": "$user_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 1}
    ]

    active_user_data = list(activity_logs.aggregate(pipeline))
    most_active_user = None

    if active_user_data:
        user_id = active_user_data[0]["_id"]
        user = users.find_one({"_id": ObjectId(user_id)})
        if user:
            most_active_user = user["name"]

    # ------------------------------
    # Last 7 Days Login Analytics
    # ------------------------------
    last_7_days = []
    login_counts = []

    for i in range(6, -1, -1):
        day = datetime.datetime.now() - datetime.timedelta(days=i)
        start = datetime.datetime(day.year, day.month, day.day)
        end = start + datetime.timedelta(days=1)

        count = activity_logs.count_documents({
            "action": "Logged in",
            "timestamp": {"$gte": start.timestamp(), "$lt": end.timestamp()}
        })

        last_7_days.append(day.strftime("%a"))
        login_counts.append(count)

    return render_template(
        "admin_analytics.html",
        total_users=total_users,
        total_materials=total_materials,
        total_downloads=total_downloads,
        top_material=top_material,
        most_active_user=most_active_user,
        last_7_days=last_7_days,
        login_counts=login_counts
    )  

# -------------------------------------------------
# ADD / REMOVE BOOKMARK
# -------------------------------------------------
@app.route("/bookmark/<file_id>")
def toggle_bookmark(file_id):
    if "user_id" not in session:
        return redirect("/")

    existing = bookmarks.find_one({
        "user_id": session["user_id"],
        "file_id": file_id
    })

    if existing:
        bookmarks.delete_one({"_id": existing["_id"]})
    else:
        bookmarks.insert_one({
            "user_id": session["user_id"],
            "file_id": file_id
        })

    return redirect(request.referrer)

# -------------------------------------------------
# BOOKMARKS PAGE
# -------------------------------------------------
@app.route("/bookmarks")
def view_bookmarks():
    if "user_id" not in session:
        return redirect("/")

    user_bookmarks = list(bookmarks.find({
        "user_id": session["user_id"]
    }))

    material_list = []

    for b in user_bookmarks:
        material = materials.find_one({
            "file_id": ObjectId(b["file_id"])
        })
        if material:
            material_list.append(material)

    return render_template("bookmarks.html", materials=material_list)      

# -------------------------------------------------
# NOTIFICATIONS
# -------------------------------------------------
def add_notification(message, user_id=None):
    notifications.insert_one({
        "message": message,
        "user_id": user_id,   # None = global notification
        "is_read": False,
        "timestamp": time.time()
    })   

# -------------------------------------------------
# GET NOTIFICATIONS (GLOBAL + USER-SPECIFIC)
# -------------------------------------------------
@app.route("/notifications")
def get_notifications():
    if "user_id" not in session:
        return []

    user_id = session["user_id"]

    user_notifications = list(
        notifications.find({
            "$or": [
                {"user_id": None},
                {"user_id": user_id}
            ]
        }).sort("timestamp", -1).limit(5)
    )

    return user_notifications       

# -------------------------------
# CHATBOT API
# -------------------------------
@app.route("/chatbot", methods=["POST"])
def chatbot():
    if "user_id" not in session:
        return {"reply": "Please login first"}

    user_msg = request.json.get("message", "").lower()

    # 1️⃣ Basic responses
    if "hello" in user_msg or "hi" in user_msg:
        return {"reply": "Hello 👋 How can I help you?"}

    if "help" in user_msg:
        return {"reply": "You can ask about subjects like DBMS, OS, Python or materials."}

    # 2️⃣ Search materials (SMART PART 🔥)
    results = list(materials.find({
        "$or": [
            {"title": {"$regex": user_msg, "$options": "i"}},
            {"subject": {"$regex": user_msg, "$options": "i"}}
        ]
    }).limit(3))

    if results:
        reply = "📚 I found these materials:\n"
        for r in results:
            reply += f"- {r['title']} ({r['subject']})\n"
        return {"reply": reply}

    # 3️⃣ Default response
    return {"reply": "Sorry 😅 I couldn't find anything. Try another keyword."}

    
# -------------------------------------------------
# LOGOUT
# -------------------------------------------------
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# -------------------------------------------------
# RUN
# -------------------------------------------------
if __name__ == "__main__":
    app.run(debug=True)
