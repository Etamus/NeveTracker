import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from routes.auth import auth_router
from services.auth_service import ensure_owner_user


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        with self.session_factory() as db:
            ensure_owner_user(db)

        app = FastAPI()
        app.include_router(auth_router, prefix="/auth")

        def override_db():
            db = self.session_factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def login(self, username="DEUSM2", password="1234"):
        response = self.client.post(
            "/auth/login",
            json={"username": username, "password": password},
        )
        self.assertEqual(response.status_code, 200)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    def test_owner_can_create_promote_update_and_remove_users(self):
        owner_headers = self.login()
        created = self.client.post(
            "/auth/users",
            headers=owner_headers,
            json={"username": "operador", "password": "5678"},
        )
        self.assertEqual(created.status_code, 201)
        user_id = created.json()["id"]

        promoted = self.client.patch(
            f"/auth/users/{user_id}",
            headers=owner_headers,
            json={"is_admin": True, "password": "8765"},
        )
        self.assertEqual(promoted.status_code, 200)
        self.assertTrue(promoted.json()["is_admin"])

        promoted_headers = self.login("OPERADOR", "8765")
        listed = self.client.get("/auth/users", headers=promoted_headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["items"]), 2)

        removed = self.client.delete(
            f"/auth/users/{user_id}",
            headers=owner_headers,
        )
        self.assertEqual(removed.status_code, 204)

    def test_only_owner_can_define_admins(self):
        owner_headers = self.login()
        created = self.client.post(
            "/auth/users",
            headers=owner_headers,
            json={"username": "gestor", "password": "5678", "is_admin": True},
        )
        admin_headers = self.login("GESTOR", "5678")

        denied = self.client.post(
            "/auth/users",
            headers=admin_headers,
            json={"username": "admin2", "password": "9999", "is_admin": True},
        )
        self.assertEqual(denied.status_code, 403)

        allowed = self.client.post(
            "/auth/users",
            headers=admin_headers,
            json={"username": "consulta", "password": "9999", "is_admin": False},
        )
        self.assertEqual(allowed.status_code, 201)

    def test_normal_user_cannot_open_administration(self):
        owner_headers = self.login()
        self.client.post(
            "/auth/users",
            headers=owner_headers,
            json={"username": "consulta", "password": "9999"},
        )
        user_headers = self.login("CONSULTA", "9999")

        response = self.client.get("/auth/users", headers=user_headers)
        self.assertEqual(response.status_code, 403)

    def test_governor_can_only_create_viewers_and_cannot_change_roles(self):
        owner_headers = self.login()
        governor = self.client.post(
            "/auth/users",
            headers=owner_headers,
            json={"username": "governante", "password": "5678", "role": "governor"},
        )
        self.assertEqual(governor.status_code, 201)
        self.assertEqual(governor.json()["role"], "governor")
        governor_headers = self.login("GOVERNANTE", "5678")

        viewer = self.client.post(
            "/auth/users",
            headers=governor_headers,
            json={"username": "visualizador", "password": "9999", "role": "viewer"},
        )
        self.assertEqual(viewer.status_code, 201)
        self.assertEqual(viewer.json()["role"], "viewer")

        denied_create = self.client.post(
            "/auth/users",
            headers=governor_headers,
            json={"username": "outrogestor", "password": "9999", "role": "governor"},
        )
        self.assertEqual(denied_create.status_code, 403)

        denied_promote = self.client.patch(
            f"/auth/users/{viewer.json()['id']}",
            headers=governor_headers,
            json={"role": "governor"},
        )
        self.assertEqual(denied_promote.status_code, 403)

    def test_deusm2_is_the_only_developer(self):
        owner_headers = self.login()
        me = self.client.get("/auth/me", headers=owner_headers)
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["role"], "developer")

        denied = self.client.post(
            "/auth/users",
            headers=owner_headers,
            json={"username": "dev2", "password": "9999", "role": "developer"},
        )
        self.assertEqual(denied.status_code, 403)

    def test_owner_can_set_and_remove_profile_photo(self):
        owner_headers = self.login()
        created = self.client.post(
            "/auth/users",
            headers=owner_headers,
            json={"username": "fotoperfil", "password": "5678"},
        )
        user_id = created.json()["id"]
        avatar = "data:image/jpeg;base64,/9j/2Q=="

        updated = self.client.patch(
            f"/auth/users/{user_id}",
            headers=owner_headers,
            json={"avatar_data_url": avatar},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["avatar_data_url"], avatar)

        removed = self.client.patch(
            f"/auth/users/{user_id}",
            headers=owner_headers,
            json={"avatar_data_url": None},
        )
        self.assertEqual(removed.status_code, 200)
        self.assertIsNone(removed.json()["avatar_data_url"])

    def test_governor_cannot_change_administrator_photo(self):
        owner_headers = self.login()
        owner_id = self.client.get("/auth/me", headers=owner_headers).json()["id"]
        created = self.client.post(
            "/auth/users", headers=owner_headers,
            json={"username": "gestorfoto", "password": "5678", "role": "governor"},
        )
        self.assertEqual(created.status_code, 201)
        governor_headers = self.login("GESTORFOTO", "5678")
        denied = self.client.patch(
            f"/auth/users/{owner_id}", headers=governor_headers,
            json={"avatar_data_url": "data:image/png;base64,iVBORw0KGgo="},
        )
        self.assertEqual(denied.status_code, 403)
        self.assertIsNone(self.client.get("/auth/me", headers=owner_headers).json()["avatar_data_url"])

    def test_owner_can_create_user_with_profile_photo(self):
        owner_headers = self.login()
        avatar = "data:image/png;base64,iVBORw0KGgo="
        created = self.client.post(
            "/auth/users",
            headers=owner_headers,
            json={
                "username": "fotocriacao",
                "password": "5678",
                "avatar_data_url": avatar,
            },
        )
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["avatar_data_url"], avatar)

    def test_rejects_invalid_profile_photo(self):
        owner_headers = self.login()
        response = self.client.patch(
            "/auth/users/1",
            headers=owner_headers,
            json={"avatar_data_url": "data:text/plain;base64,SGVsbG8="},
        )
        self.assertEqual(response.status_code, 400)

    def test_administrator_has_full_access_and_can_assign_profiles(self):
        from routes.integrations import integration_router
        from routes.products import product_router
        self.client.app.include_router(integration_router,prefix='/integrations')
        self.client.app.include_router(product_router,prefix='/products')
        owner=self.login()
        created=self.client.post('/auth/users',headers=owner,json={'username':'ADMIN2','password':'5678','role':'administrator'})
        self.assertEqual(created.status_code,201,created.text)
        self.assertEqual(created.json()['role'],'administrator')
        self.assertFalse(created.json()['is_owner'])
        admin=self.login('ADMIN2','5678')
        self.assertEqual(self.client.get('/integrations',headers=admin).status_code,200)
        self.assertEqual(self.client.get('/products/crawler-control',headers=admin).status_code,200)
        control=self.client.put('/products/crawler-control',headers=admin,json={'default_mode':'precise'})
        self.assertEqual(control.status_code,200,control.text)
        subordinate=self.client.post('/auth/users',headers=admin,json={'username':'ADMIN3','password':'5678','role':'administrator'})
        self.assertEqual(subordinate.status_code,201,subordinate.text)
        target=subordinate.json()['id']
        self.assertEqual(self.client.patch(f'/auth/users/{target}',headers=admin,json={'role':'viewer'}).status_code,200)
        self.assertEqual(self.client.patch(f'/auth/users/{target}',headers=admin,json={'role':'administrator'}).status_code,200)
        self.assertEqual(self.client.patch('/auth/users/1',headers=admin,json={'role':'viewer'}).status_code,400)
        self.client.post('/auth/users',headers=owner,json={'username':'GOVTEST','password':'5678','role':'governor'})
        gov=self.login('GOVTEST','5678')
        self.assertEqual(self.client.post('/auth/users',headers=gov,json={'username':'ESCALATE','password':'5678','role':'administrator'}).status_code,403)
        self.assertEqual(self.client.patch(f'/auth/users/{target}',headers=gov,json={'role':'administrator'}).status_code,403)
        self.assertEqual(self.client.get('/integrations',headers=gov).status_code,403)


if __name__ == "__main__":
    unittest.main()
