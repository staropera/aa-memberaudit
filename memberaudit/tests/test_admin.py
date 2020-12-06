from unittest.mock import patch

from django.test import TestCase
from django.contrib.admin.sites import AdminSite

from . import create_user_from_evecharacter
from ..admin import SkillSetAdmin
from ..models import SkillSet
from .testdata.load_entities import load_entities

MODULE_PATH = "memberaudit.admin"


class MockRequest(object):
    def __init__(self, user=None):
        self.user = user


class TestDoctrineShipAdmin(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.modeladmin = SkillSetAdmin(model=SkillSet, admin_site=AdminSite())
        load_entities()
        cls.user = create_user_from_evecharacter(1001)

    @patch(MODULE_PATH + ".tasks.update_characters_skill_checks")
    def test_save_model(self, mock_update_characters_skill_checks):
        ship = SkillSet.objects.create(name="Dummy")
        request = MockRequest(self.user)
        form = self.modeladmin.get_form(request)
        self.modeladmin.save_model(request, ship, form, True)

        self.assertTrue(mock_update_characters_skill_checks.delay.called)

    @patch(MODULE_PATH + ".tasks.update_characters_skill_checks")
    def test_delete_model(self, mock_update_characters_skill_checks):
        ship = SkillSet.objects.create(name="Dummy")
        request = MockRequest(self.user)
        self.modeladmin.delete_model(request, ship)

        self.assertTrue(mock_update_characters_skill_checks.delay.called)