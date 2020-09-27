from django.http import HttpResponse
from django.test import TestCase, RequestFactory

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.tests.auth_utils import AuthUtils

from ..decorators import fetch_owner_if_allowed
from ..models import Owner, OwnerSkills
from ..utils import generate_invalid_pk


DUMMY_URL = "http://www.example.com"


class TestFetchOwnerIfAllowed(TestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.user = AuthUtils.create_user("Bruce Wanye")
        character = AuthUtils.add_main_character_2(self.user, "Bruce Wayne", 1001)
        character_ownership = CharacterOwnership.objects.create(
            user=self.user, character=character, owner_hash="123456"
        )
        self.owner = Owner.objects.create(character_ownership=character_ownership)

    def test_passthrough_when_fetch_owner_if_allowed(self):
        @fetch_owner_if_allowed()
        def dummy(request, owner_pk, owner):
            self.assertEqual(owner, self.owner)
            self.assertIn("character_ownership", owner._state.fields_cache)
            return HttpResponse("ok")

        request = self.factory.get(DUMMY_URL)
        request.user = self.user
        response = dummy(request, self.owner.pk)
        self.assertEqual(response.status_code, 200)

    def test_returns_404_when_owner_not_found(self):
        @fetch_owner_if_allowed()
        def dummy(request, owner_pk, owner):
            self.assertTrue(False)

        request = self.factory.get(DUMMY_URL)
        request.user = self.user
        response = dummy(request, generate_invalid_pk(Owner))
        self.assertEqual(response.status_code, 404)

    def test_returns_403_when_user_has_not_access(self):
        @fetch_owner_if_allowed()
        def dummy(request, owner_pk, owner):
            self.assertTrue(False)

        user_2 = AuthUtils.create_user("Lex Luthor")
        request = self.factory.get(DUMMY_URL)
        request.user = user_2
        response = dummy(request, self.owner.pk)
        self.assertEqual(response.status_code, 403)

    def test_can_specify_list_for_select_related(self):
        @fetch_owner_if_allowed("skills")
        def dummy(request, owner_pk, owner):
            self.assertEqual(owner, self.owner)
            self.assertIn("skills", owner._state.fields_cache)
            return HttpResponse("ok")

        OwnerSkills.objects.create(owner=self.owner, total_sp=10000000)
        request = self.factory.get(DUMMY_URL)
        request.user = self.user
        dummy(request, self.owner.pk)

    """
    def test_normal_fetch_owner_pk_from_session(self):
        @fetch_owner_if_allowed()
        def dummy(request, owner):
            self.assertEqual(owner, self.owner)
            return HttpResponse("ok")

        request = self.factory.get(DUMMY_URL)
        request.user = self.user
        response = dummy(request, self.owner.pk)
        self.assertEqual(response.status_code, 200)
    """