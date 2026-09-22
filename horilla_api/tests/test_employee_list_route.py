"""GET /api/employee/employees/ lists, POST creates, GET with pk still details."""

from django.test import TestCase
from rest_framework.test import APIClient

from horilla.testkit import make_company, make_employee, make_user


class EmployeesRouteTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.company = make_company("Route Co")
        self.user = make_user("route_admin", password="secret123", is_superuser=True)
        self.employee = make_employee(
            company=self.company,
            email="route_admin@test.horilla",
            first_name="Route",
            last_name="Admin",
            user=self.user,
        )
        login = self.client.post(
            "/api/auth/login/",
            {"username": "route_admin", "password": "secret123"},
            format="json",
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")

    def test_get_without_pk_returns_paginated_list(self):
        response = self.client.get("/api/employee/employees/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("results", response.data)
        ids = [row["id"] for row in response.data["results"]]
        self.assertIn(self.employee.pk, ids)

    def test_get_without_pk_matches_list_endpoint(self):
        new_route = self.client.get("/api/employee/employees/")
        legacy_route = self.client.get("/api/employee/list/employees/")
        self.assertEqual(new_route.status_code, 200)
        self.assertEqual(new_route.data, legacy_route.data)

    def test_get_with_pk_returns_detail(self):
        response = self.client.get(f"/api/employee/employees/{self.employee.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["id"], self.employee.pk)
        self.assertEqual(response.data["employee_first_name"], "Route")

    def test_post_creates_employee(self):
        response = self.client.post(
            "/api/employee/employees/",
            {
                "employee_first_name": "Nueva",
                "employee_last_name": "Persona",
                "email": "nueva@test.horilla",
                "phone": "5551234567",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["employee_first_name"], "Nueva")
