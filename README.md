
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit
import os

app = Flask(__name__)
app.config["SECRET_KEY"] = "sutda"

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")


@app.route("/")
def index():
    return "SUTDA SERVER RUNNING"


@socketio.on("connect")
def connect():
    print("connected:", request.sid)
    emit("msg", {"text": "connected"})


@socketio.on("action")
def action(data):
    print("action:", data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port)
