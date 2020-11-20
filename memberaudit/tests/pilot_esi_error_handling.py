# flake8: noqa
"""
This is a standalone scripts that creates invalid structure ids
in order to test the ESI error rate limit handling for structures
"""

from datetime import timedelta
import inspect
import json
import os
import sys

currentdir = os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe())))
myauth_dir = os.path.dirname(os.path.dirname(os.path.dirname(currentdir))) + "/myauth"
sys.path.insert(0, myauth_dir)


import django
from django.db import transaction
from django.apps import apps
from django.utils.timezone import now

# init and setup django project
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "myauth.settings.local")
django.setup()

if not apps.is_installed("memberaudit"):
    raise RuntimeError("The app memberaudit is not installed")

import random
from memberaudit.models import Location, Character

print("Trying to resolve multiple random structure IDs...")
character = Character.objects.first()
token = character.fetch_token()
base_structure_id = 1000000000000

for _ in range(10):
    random_structure_id = random.randint(1000000000000, 1100000000000)
    Location.objects.update_or_create_esi_async(id=random_structure_id, token=token)

print("Completed.")
