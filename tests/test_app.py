"""Tests for ARTwrks: login, ownership of portfolios/artworks, passwords and signup.

Uses a separate test database and moto's fake S3, so no real AWS calls are made.

Setup (once):
    pip3 install -r requirements-dev.txt
    createdb artworks_test

Run from the project folder:
    pytest
"""
import os
import sys

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
# make the project folder importable (server.py, model.py, crud_*.py)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import boto3
import pytest
from moto import mock_s3

BUCKET = os.environ.get("S3_BUCKET_NAME", "artworks-images")
PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="


@pytest.fixture(scope="session")
def s3():
    with mock_s3():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        yield boto3.client("s3", region_name="us-east-1")


@pytest.fixture(scope="session")
def app(s3):
    import server
    from model import connect_to_db
    server.app.config["TESTING"] = True
    connect_to_db(server.app, "postgresql:///artworks_test", echo=False)
    return server.app


@pytest.fixture(autouse=True)
def fresh_db(app):
    from model import db
    for key in s3_keys(boto3.client("s3", region_name="us-east-1")):
        boto3.client("s3", region_name="us-east-1").delete_object(Bucket=BUCKET, Key=key)
    with app.app_context():
        db.drop_all()
        db.create_all()
    yield


def signup(app, name):
    client = app.test_client()
    client.post("/create-user", data={"username": name, "email": f"{name}@x.com", "password": "pw123456"})
    return client


def save_art(client, title, new_portfolio=None, portfolio_id=None):
    body = {"artwork-title": title, "artwork-dataURL": PNG}
    if portfolio_id:
        body.update({"portfolio-id": portfolio_id, "portfolio-title": "existing"})
    else:
        body["new-portfolio-title"] = new_portfolio
    return client.post("/api/save-artwork", json=body).get_json()


def bobs_data(app):
    """Bob signs up and saves one artwork in a new portfolio. Returns (bob, pid, artwork)."""
    from model import Artwork, Portfolio
    bob = signup(app, "bob")
    save_art(bob, "sunset", new_portfolio="bob-folio")
    with app.app_context():
        p = Portfolio.query.filter_by(p_title="bob-folio").one()
        a = Artwork.query.filter_by(a_title="sunset").one()
        return bob, p.portfolio_id, (a.artwork_id, a.file_path)


def counts(app):
    from model import Artwork, Portfolio
    with app.app_context():
        return Portfolio.query.count(), Artwork.query.count()


def s3_keys(s3):
    return [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]


# ---------- the owner can still do everything ----------

def test_owner_can_manage_own_work(app, s3):
    bob, pid, (aid, path) = bobs_data(app)
    assert bob.get(f"/api/get-portfolio-artworks/{pid}").get_json()["message"][0][0] == "sunset"
    assert bob.post(f"/api/update-portfolio-title/{pid}", json={"title": "renamed"}).get_json()["status"] == "success"
    assert bob.post(f"/api/update-artwork-title/{aid}/{pid}", json={"title": "dawn"}).get_json()["status"] == "success"
    r = bob.post(f"/api/update-artwork-new-portfolio/{aid}", json={"pTitle": "second"}).get_json()
    assert r["status"] == "success"
    assert bob.post(f"/api/update-artwork-portfolio-id/{aid}", json={"pId": pid}).get_json()["status"] == "success"
    assert save_art(bob, "moon", portfolio_id=pid)["status"] == "success"
    assert len(s3_keys(s3)) >= 2
    assert bob.post(f"/api/delete-artwork/{aid}", json={"path": path}).get_json()["status"] == "success"
    assert bob.get(f"/api/delete-porfolio/{pid}").get_json()["status"] == "success"


# ---------- another user cannot touch Bob's work ----------

def test_other_user_cannot_view_bobs_portfolio(app):
    _, pid, _ = bobs_data(app)
    amy = signup(app, "amy")
    r = amy.get(f"/api/get-portfolio-artworks/{pid}").get_json()
    assert r["status"] != "success"


def test_other_user_cannot_rename_or_delete_bobs_portfolio(app):
    _, pid, _ = bobs_data(app)
    amy = signup(app, "amy")
    amy.post(f"/api/update-portfolio-title/{pid}", json={"title": "hacked"})
    amy.get(f"/api/delete-porfolio/{pid}")
    from model import Portfolio
    with app.app_context():
        assert Portfolio.query.get(pid).p_title == "bob-folio"


def test_other_user_cannot_change_or_delete_bobs_artwork(app, s3):
    _, pid, (aid, path) = bobs_data(app)
    amy = signup(app, "amy")
    amy.post(f"/api/update-artwork-title/{aid}/{pid}", json={"title": "hacked"})
    amy.post(f"/api/update-artwork-new-portfolio/{aid}", json={"pTitle": "amy-steals"})
    amy.post(f"/api/delete-artwork/{aid}", json={"path": path})
    from model import Artwork
    with app.app_context():
        a = Artwork.query.get(aid)
        assert a is not None and a.a_title == "sunset" and a.portfolio_id == pid
    assert len(s3_keys(s3)) >= 1


def test_cannot_move_artwork_into_someone_elses_portfolio(app):
    _, bob_pid, _ = bobs_data(app)
    amy = signup(app, "amy")
    save_art(amy, "amys-art", new_portfolio="amy-folio")
    from model import Artwork
    with app.app_context():
        amy_aid = Artwork.query.filter_by(a_title="amys-art").one().artwork_id
    amy.post(f"/api/update-artwork-portfolio-id/{amy_aid}", json={"pId": bob_pid})
    r = save_art(amy, "sneaky", portfolio_id=bob_pid)
    with app.app_context():
        assert Artwork.query.get(amy_aid).portfolio_id != bob_pid
        assert Artwork.query.filter_by(portfolio_id=bob_pid).count() == 1


def test_delete_artwork_ignores_client_sent_path(app, s3):
    """Deleting your own artwork must not let you delete a different S3 file."""
    bob, _, _ = bobs_data(app)
    bob_key = s3_keys(s3)[0]
    amy = signup(app, "amy")
    save_art(amy, "amys-art", new_portfolio="amy-folio")
    from model import Artwork
    with app.app_context():
        amy_aid = Artwork.query.filter_by(a_title="amys-art").one().artwork_id
    amy.post(f"/api/delete-artwork/{amy_aid}",
             json={"path": f"https://{BUCKET}.s3.amazonaws.com/{bob_key}"})
    assert bob_key in s3_keys(s3)


# ---------- logged-out requests are refused cleanly ----------

@pytest.mark.parametrize("method,url", [
    ("get", "/api/user-portfolio-titles"),
    ("get", "/api/get-user-artworks"),
    ("get", "/api/get-portfolio-artworks/1"),
    ("get", "/api/delete-porfolio/1"),
    ("post", "/api/search-artworks"),
])
def test_logged_out_requests_get_401_not_crash(app, method, url):
    bobs_data(app)
    anon = app.test_client()
    r = getattr(anon, method)(url, json={"searchParam": "s"} if method == "post" else None)
    assert r.status_code == 401
    assert counts(app) == (1, 1)


# ---------- passwords ----------

def test_password_is_stored_with_a_slow_salted_hash(app):
    import hashlib
    signup(app, "carol")
    from model import User
    with app.app_context():
        stored = User.query.filter_by(email="carol@x.com").one().password
    assert stored != hashlib.sha256(b"pw123456").hexdigest()
    assert stored.startswith(("pbkdf2:", "scrypt:"))


def test_login_with_right_and_wrong_password(app):
    signup(app, "dave")
    client = app.test_client()
    bad = client.post("/login", data={"email": "dave@x.com", "password": "wrong-pass"})
    assert bad.status_code == 200 and b"Incorrect login" in bad.data
    good = client.post("/login", data={"email": "dave@x.com", "password": "pw123456"})
    assert good.status_code == 302 and good.headers["Location"].endswith("/user")


# ---------- data model ----------

def test_signup_with_a_normal_length_email(app):
    client = app.test_client()
    email = "firstname.lastname@gmail.com"   # 28 characters
    r = client.post("/create-user", data={"username": "fl", "email": email, "password": "pw123456"})
    assert r.status_code == 302
    from model import User
    with app.app_context():
        assert User.query.filter_by(email=email).count() == 1


def test_two_users_can_have_portfolios_with_the_same_name(app):
    bob = signup(app, "bob")
    amy = signup(app, "amy")
    assert save_art(bob, "a", new_portfolio="Sketches")["status"] == "success"
    assert save_art(amy, "b", new_portfolio="Sketches")["status"] == "success"


# ---------- signup ----------

def test_signup_page_loads(app):
    r = app.test_client().get("/signup")
    assert r.status_code == 200 and b"REGISTER" in r.data


def test_duplicate_email_shows_message_instead_of_crashing(app):
    signup(app, "erin")
    r = app.test_client().post("/create-user", data={"username": "erin2", "email": "erin@x.com", "password": "pw123456"})
    assert r.status_code == 200 and b"already exists" in r.data
    from model import User
    with app.app_context():
        assert User.query.filter_by(email="erin@x.com").count() == 1


@pytest.mark.parametrize("form,msg", [
    ({"username": "", "email": "a@x.com", "password": "pw123456"}, b"fill in all fields"),
    ({"username": "a", "email": "", "password": "pw123456"}, b"fill in all fields"),
    ({"username": "a", "email": "a@x.com", "password": ""}, b"fill in all fields"),
    ({"username": "a", "email": "not-an-email", "password": "pw123456"}, b"valid email"),
])
def test_signup_rejects_missing_or_bad_fields(app, form, msg):
    r = app.test_client().post("/create-user", data=form)
    assert r.status_code == 200 and msg in r.data
    from model import User
    with app.app_context():
        assert User.query.count() == 0
