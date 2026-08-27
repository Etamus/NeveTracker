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


if __name__ == "__main__":
    unittest.main()
