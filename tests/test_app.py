import os,tempfile,pytest
from app import app,init_db
@pytest.fixture()
def client():
 fd,path=tempfile.mkstemp();os.close(fd);app.config.update(TESTING=True,DATABASE=path,SECRET_KEY='test')
 with app.app_context():init_db()
 with app.test_client() as c:yield c
 os.unlink(path)
def login(c):return c.post('/login',data={'username':'admin','password':'admin123'},follow_redirects=True)
def test_login_dashboard(client):assert b'Dashboard' in login(client).data
def test_protected(client):assert client.get('/').status_code==302
def test_empty_registration(client):login(client);assert client.post('/students/register',json={}).status_code==400
def test_short_liveness_capture(client):login(client);assert client.post('/recognize',json={'frames':[]}).status_code==400
def test_csv_export(client):login(client);r=client.get('/attendance/export');assert r.status_code==200 and r.mimetype=='text/csv'
