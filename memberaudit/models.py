import json
from typing import Optional

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.utils.html import strip_tags
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from esi.models import Token

from eveuniverse.models import (
    EveAncestry,
    EveBloodline,
    EveEntity,
    EveFaction,
    EveRace,
    EveSolarSystem,
    EveStation,
    EveType,
)

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.services.hooks import get_extension_logger

from . import __title__
from .app_settings import MEMBERAUDIT_MAX_MAILS, MEMBERAUDIT_DEVELOPER_MODE
from .decorators import fetch_token
from .managers import CharacterManager
from .providers import esi
from .utils import LoggerAddTag, make_logger_prefix


logger = LoggerAddTag(get_extension_logger(__name__), __title__)

CURRENCY_MAX_DIGITS = 17


def eve_xml_to_html(xml: str) -> str:
    x = xml.replace("<br>", "\n")
    x = strip_tags(x)
    x = x.replace("\n", "<br>")
    return mark_safe(x)


class Memberaudit(models.Model):
    """Meta model for app permissions"""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            ("basic_access", "Can access this app"),
            ("unrestricted_access", "Can view all characters and data"),
        )


class Character(models.Model):
    """A character synced by this app"""

    character_ownership = models.OneToOneField(
        CharacterOwnership,
        related_name="memberaudit_character",
        on_delete=models.CASCADE,
        help_text="character registered to member audit",
    )

    total_sp = models.BigIntegerField(
        validators=[MinValueValidator(0)], default=None, null=True
    )
    unallocated_sp = models.PositiveIntegerField(default=None, null=True)
    wallet_balance = models.DecimalField(
        max_digits=CURRENCY_MAX_DIGITS,
        decimal_places=2,
        default=None,
        null=True,
        blank=True,
    )
    total_unread_count = models.PositiveIntegerField(default=None, null=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = CharacterManager()

    def __str__(self):
        return str(self.character_ownership)

    def user_has_access(self, user: User) -> bool:
        """Returns True if given user has permission to view this character"""
        if self.character_ownership.user == user:
            return True
        elif user.has_perm("memberaudit.unrestricted_access"):
            return True

        return False

    def is_update_status_ok(self) -> bool:
        """returns status of last update

        Returns:
        - True: If update was complete and without errors
        - False if there where any errors
        - None: if last update is incomplete
        """
        errors_count = self.sync_status_set.filter(sync_ok=False).count()
        ok_count = self.sync_status_set.filter(sync_ok=True).count()
        if errors_count > 0:
            return False
        elif ok_count == len(CharacterSyncStatus.TOPIC_CHOICES):
            return True
        else:
            return None

    def update_character_details(self):
        """syncs the character details for the given character"""
        add_prefix = make_logger_prefix(self)
        logger.info(add_prefix("Fetching character details from ESI"))
        details = esi.client.Character.get_characters_character_id(
            character_id=self.character_ownership.character.character_id,
        ).results()
        if details.get("alliance_id"):
            alliance, _ = EveEntity.objects.get_or_create_esi(
                id=details.get("alliance_id")
            )
        else:
            alliance = None

        if details.get("ancestry_id"):
            eve_ancestry, _ = EveAncestry.objects.get_or_create_esi(
                id=details.get("ancestry_id")
            )
        else:
            eve_ancestry = None

        eve_bloodline, _ = EveBloodline.objects.get_or_create_esi(
            id=details.get("bloodline_id")
        )
        corporation, _ = EveEntity.objects.get_or_create_esi(
            id=details.get("corporation_id")
        )
        description = details.get("description") if details.get("description") else ""
        if details.get("faction_id"):
            faction, _ = EveFaction.objects.get_or_create_esi(
                id=details.get("faction_id")
            )
        else:
            faction = None

        if details.get("gender") == "male":
            gender = CharacterDetails.GENDER_MALE
        else:
            gender = CharacterDetails.GENDER_FEMALE

        race, _ = EveRace.objects.get_or_create_esi(id=details.get("race_id"))
        title = details.get("title") if details.get("title") else ""
        CharacterDetails.objects.update_or_create(
            character=self,
            defaults={
                "alliance": alliance,
                "birthday": details.get("birthday"),
                "eve_ancestry": eve_ancestry,
                "eve_bloodline": eve_bloodline,
                "eve_faction": faction,
                "eve_race": race,
                "corporation": corporation,
                "description": description,
                "gender": gender,
                "name": details.get("name"),
                "security_status": details.get("security_status"),
                "title": title,
            },
        )

    def update_corporation_history(self):
        """syncs the character's corporation history"""
        add_prefix = make_logger_prefix(self)
        logger.info(add_prefix("Fetching corporation history from ESI"))
        history = esi.client.Character.get_characters_character_id_corporationhistory(
            character_id=self.character_ownership.character.character_id,
        ).results()
        for row in history:
            corporation, _ = EveEntity.objects.get_or_create_esi(
                id=row.get("corporation_id")
            )
            CharacterCorporationHistory.objects.update_or_create(
                character=self,
                record_id=row.get("record_id"),
                defaults={
                    "corporation": corporation,
                    "is_deleted": row.get("is_deleted"),
                    "start_date": row.get("start_date"),
                },
            )

    @fetch_token("esi-mail.read_mail.v1")
    def update_mails(self, token):
        self._update_mailinglists(token)
        self._update_maillabels(token)
        self._update_mails(token)

    def _update_mailinglists(self, token: Token):
        """syncs the mailing list for the given character"""
        add_prefix = make_logger_prefix(self)
        logger.info(add_prefix("Fetching mailing lists from ESI"))

        mailing_lists = esi.client.Mail.get_characters_character_id_mail_lists(
            character_id=self.character_ownership.character.character_id,
            token=token.valid_access_token(),
        ).results()

        logger.info(
            add_prefix("Received {} mailing lists from ESI".format(len(mailing_lists)))
        )

        created_count = 0
        for mailing_list in mailing_lists:
            _, created = CharacterMailingList.objects.update_or_create(
                character=self,
                list_id=mailing_list.get("mailing_list_id"),
                defaults={"name": mailing_list.get("name")},
            )
            if created:
                created_count += 1

        if created_count > 0:
            logger.info(
                add_prefix("Added/Updated {} mailing lists".format(created_count))
            )

    def _update_maillabels(self, token: Token):
        """syncs the mail lables for the given character"""
        add_prefix = make_logger_prefix(self)
        logger.info(add_prefix("Fetching mail labels from ESI"))

        mail_labels_info = esi.client.Mail.get_characters_character_id_mail_labels(
            character_id=self.character_ownership.character.character_id,
            token=token.valid_access_token(),
        ).results()
        mail_labels = mail_labels_info.get("labels")
        logger.info(
            add_prefix("Received {} mail labels from ESI".format(len(mail_labels)))
        )
        self.total_unread_count = mail_labels_info.get("total_unread_count")
        self.save()
        created_count = 0
        for label in mail_labels:
            _, created = CharacterMailLabel.objects.update_or_create(
                character=self,
                label_id=label.get("label_id"),
                defaults={
                    "color": label.get("color"),
                    "name": label.get("name"),
                    "unread_count": label.get("unread_count"),
                },
            )
            if created:
                created_count += 1

        if created_count > 0:
            logger.info(
                add_prefix("Added/Updated {} mail labels".format(created_count))
            )

    def _update_mails(self, token: Token):
        add_prefix = make_logger_prefix(self)

        # fetch mail headers
        last_mail_id = None
        mail_headers_all = list()
        page = 1

        while True:
            logger.info(
                add_prefix("Fetching mail headers from ESI - page {}".format(page))
            )
            mail_headers = esi.client.Mail.get_characters_character_id_mail(
                character_id=self.character_ownership.character.character_id,
                last_mail_id=last_mail_id,
                token=token.valid_access_token(),
            ).results()

            mail_headers_all += mail_headers

            if len(mail_headers) < 50 or len(mail_headers_all) >= MEMBERAUDIT_MAX_MAILS:
                break
            else:
                last_mail_id = min([x["mail_id"] for x in mail_headers])
                page += 1

        logger.info(
            add_prefix(
                "Received {} mail headers from ESI".format(len(mail_headers_all))
            )
        )

        if MEMBERAUDIT_DEVELOPER_MODE:
            # store to disk (for debugging)
            with open(
                "mail_headers_raw_{}.json".format(
                    self.character_ownership.character.character_id
                ),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(
                    mail_headers_all, f, cls=DjangoJSONEncoder, sort_keys=True, indent=4
                )

        # update IDs from ESI
        ids = set()
        mailing_list_ids = [
            x["list_id"]
            for x in CharacterMailingList.objects.filter(character=self)
            .select_related()
            .values("list_id")
        ]
        for header in mail_headers_all:
            if header.get("from") not in mailing_list_ids:
                ids.add(header.get("from"))
            for recipient in header.get("recipients", []):
                if recipient.get("recipient_type") != "mailing_list":
                    ids.add(recipient.get("recipient_id"))

        EveEntity.objects.bulk_create_esi(ids)

        logger.info(
            add_prefix(
                "Updating {} mail headers and loading mail bodies".format(
                    len(mail_headers_all)
                )
            )
        )

        # load mail headers
        body_count = 0
        for header in mail_headers_all:
            with transaction.atomic():
                try:
                    from_mailing_list = CharacterMailingList.objects.get(
                        list_id=header.get("from")
                    )
                    from_entity = None
                except CharacterMailingList.DoesNotExist:
                    from_entity, _ = EveEntity.objects.get_or_create_esi(
                        id=header.get("from")
                    )
                    from_mailing_list = None

                mail_obj, _ = CharacterMail.objects.update_or_create(
                    character=self,
                    mail_id=header.get("mail_id"),
                    defaults={
                        "from_entity": from_entity,
                        "from_mailing_list": from_mailing_list,
                        "is_read": bool(header.get("is_read")),
                        "subject": header.get("subject", ""),
                        "timestamp": header.get("timestamp"),
                    },
                )
                CharacterMailRecipient.objects.filter(mail=mail_obj).delete()
                for recipient in header.get("recipients"):
                    recipient_id = recipient.get("recipient_id")
                    if recipient.get("recipient_type") != "mailing_list":
                        eve_entity, _ = EveEntity.objects.get_or_create_esi(
                            id=recipient_id
                        )
                        CharacterMailRecipient.objects.create(
                            mail=mail_obj, eve_entity=eve_entity
                        )
                    else:
                        try:
                            mailing_list = self.mailing_lists.get(list_id=recipient_id)
                        except (CharacterMailingList.DoesNotExist, ObjectDoesNotExist):
                            logger.warning(
                                f"{self}: Unknown mailing list with "
                                f"id {recipient_id} for mail id {mail_obj.mail_id}",
                            )
                        else:
                            CharacterMailRecipient.objects.create(
                                mail=mail_obj, mailing_list=mailing_list
                            )

                CharacterMailMailLabel.objects.filter(mail=mail_obj).delete()
                for label_id in header.get("labels", []):
                    try:
                        label = self.mail_labels.get(label_id=label_id)
                        CharacterMailMailLabel.objects.create(
                            mail=mail_obj, label=label
                        )
                    except CharacterMailLabel.DoesNotExist:
                        logger.warning(
                            add_prefix(f"Could not find label with id {label_id}")
                        )

                if mail_obj.body is None:
                    logger.debug(
                        add_prefix(
                            "Fetching body from ESI for mail ID {}".format(
                                mail_obj.mail_id
                            )
                        )
                    )
                    mail = esi.client.Mail.get_characters_character_id_mail_mail_id(
                        character_id=self.character_ownership.character.character_id,
                        mail_id=mail_obj.mail_id,
                        token=token.valid_access_token(),
                    ).result()
                    mail_obj.body = mail.get("body", "")
                    mail_obj.save()
                    body_count += 1

        if body_count > 0:
            logger.info("loaded {} mail bodies".format(body_count))

    @fetch_token("esi-skills.read_skills.v1")
    def update_skills(self, token):
        """syncs the character's skill"""
        add_prefix = make_logger_prefix(self)
        logger.info(add_prefix("Fetching skills from ESI"))

        skills = esi.client.Skills.get_characters_character_id_skills(
            character_id=self.character_ownership.character.character_id,
            token=token.valid_access_token(),
        ).results()
        self.total_sp = skills.get("total_sp")
        self.unallocated_sp = skills.get("unallocated_sp")
        self.save()

        with transaction.atomic():
            CharacterSkill.objects.filter(character=self).delete()
            for skill in skills.get("skills"):
                eve_type, _ = EveType.objects.get_or_create_esi(
                    id=skill.get("skill_id")
                )
                CharacterSkill.objects.create(
                    character=self,
                    eve_type=eve_type,
                    active_skill_level=skill.get("active_skill_level"),
                    skillpoints_in_skill=skill.get("skillpoints_in_skill"),
                    trained_skill_level=skill.get("trained_skill_level"),
                )

    @fetch_token("esi-wallet.read_character_wallet.v1")
    def update_wallet_balance(self, token):
        """syncs the character's wallet balance"""
        add_prefix = make_logger_prefix(self)
        logger.info(add_prefix("Fetching corporation history from ESI"))
        balance = esi.client.Wallet.get_characters_character_id_wallet(
            character_id=self.character_ownership.character.character_id,
            token=token.valid_access_token(),
        ).results()
        self.wallet_balance = balance
        self.save()

    @fetch_token("esi-wallet.read_character_wallet.v1")
    def update_wallet_journal(self, token):
        """syncs the character's wallet journal"""
        add_prefix = make_logger_prefix(self)
        logger.info(add_prefix("Fetching wallet journal from ESI"))

        journal = esi.client.Wallet.get_characters_character_id_wallet_journal(
            character_id=self.character_ownership.character.character_id,
            token=token.valid_access_token(),
        ).results()
        for row in journal:
            if row.get("first_party_id"):
                first_party, _ = EveEntity.objects.get_or_create_esi(
                    id=row.get("first_party_id")
                )
            else:
                first_party = None

            if row.get("second_party_id"):
                second_party, _ = EveEntity.objects.get_or_create_esi(
                    id=row.get("second_party_id")
                )
            else:
                second_party = None

            CharacterWalletJournalEntry.objects.update_or_create(
                character=self,
                entry_id=row.get("id"),
                defaults={
                    "amount": row.get("amount"),
                    "balance": row.get("balance"),
                    "context_id": row.get("context_id"),
                    "context_id_type": CharacterWalletJournalEntry.match_context_type_id(
                        row.get("context_id_type")
                    ),
                    "date": row.get("date"),
                    "description": row.get("description"),
                    "first_party": first_party,
                    "ref_type": row.get("ref_type"),
                    "second_party": second_party,
                    "tax": row.get("tax"),
                    "tax_receiver": row.get("tax_receiver"),
                },
            )

    @fetch_token("esi-location.read_location.v1")
    def fetch_location(self, token) -> Optional[dict]:
        add_prefix = make_logger_prefix(self)
        logger.info(add_prefix("Fetching character location ESI"))
        location_info = esi.client.Location.get_characters_character_id_location(
            character_id=self.character_ownership.character.character_id,
            token=token.valid_access_token(),
        ).results()

        solar_system, _ = EveSolarSystem.objects.get_or_create_esi(
            id=location_info.get("solar_system_id")
        )
        if location_info.get("station_id"):
            station, _ = EveStation.objects.get_or_create_esi(
                id=location_info.get("station_id")
            )
        else:
            station = None

        return solar_system, station

    @classmethod
    def get_esi_scopes(cls) -> list:
        return [
            "esi-assets.read_assets.v1",
            "esi-characters.read_blueprints.v1",
            "esi-characters.read_contacts.v1",
            "esi-characters.read_fw_stats.v1",
            "esi-characters.read_loyalty.v1",
            "esi-characters.read_medals.v1",
            "esi-characters.read_notifications.v1",
            "esi-characters.read_opportunities.v1",
            "esi-characters.read_standings.v1",
            "esi-characters.read_titles.v1",
            "esi-clones.read_clones.v1",
            "esi-clones.read_implants.v1",
            "esi-contracts.read_character_contracts.v1",
            "esi-industry.read_character_jobs.v1",
            "esi-industry.read_character_mining.v1",
            "esi-location.read_location.v1",
            "esi-location.read_online.v1",
            "esi-location.read_ship_type.v1",
            "esi-mail.read_mail.v1",
            "esi-markets.read_character_orders.v1",
            "esi-markets.structure_markets.v1",
            "esi-planets.manage_planets.v1",
            "esi-search.search_structures.v1",
            "esi-skills.read_skillqueue.v1",
            "esi-skills.read_skills.v1",
            "esi-universe.read_structures.v1",
            "esi-wallet.read_character_wallet.v1",
        ]


class CharacterSyncStatus(models.Model):
    """Sync status for a character"""

    TOPIC_CHARACTER_DETAILS = "CD"
    TOPIC_CORPORATION_HISTORY = "CH"
    TOPIC_MAILS = "MA"
    TOPIC_SKILLS = "SK"
    TOPIC_WALLET_BALLANCE = "WB"
    TOPIC_WALLET_JOURNAL = "WJ"
    TOPIC_CHOICES = (
        (TOPIC_CHARACTER_DETAILS, _("character details")),
        (TOPIC_CORPORATION_HISTORY, _("corporation history")),
        (TOPIC_MAILS, _("mails")),
        (TOPIC_SKILLS, _("skills")),
        (TOPIC_WALLET_BALLANCE, _("wallet balance")),
        (TOPIC_WALLET_JOURNAL, _("wallet journal")),
    )
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="sync_status_set"
    )
    topic = models.CharField(max_length=2, choices=TOPIC_CHOICES)
    sync_ok = models.BooleanField(db_index=True)
    error_message = models.TextField(default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["character", "topic"],
                name="functional_pk_charactersyncstatus",
            )
        ]

    @classmethod
    def method_name_to_topic(cls, method_name):
        my_map = {
            "update_character_details": cls.TOPIC_CHARACTER_DETAILS,
            "update_corporation_history": cls.TOPIC_CORPORATION_HISTORY,
            "update_mails": cls.TOPIC_MAILS,
            "update_skills": cls.TOPIC_SKILLS,
            "update_wallet_balance": cls.TOPIC_WALLET_BALLANCE,
            "update_wallet_journal": cls.TOPIC_WALLET_JOURNAL,
        }
        return my_map[method_name]

    def __str__(self):
        return f"{self.character}-{self.get_topic_display()}"


class CharacterDetails(models.Model):
    """Details for a character"""

    GENDER_MALE = "m"
    GENDER_FEMALE = "f"
    GENDER_CHOICES = (
        (GENDER_MALE, _("male")),
        (GENDER_FEMALE, _("female")),
    )
    character = models.OneToOneField(
        Character,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="details",
        help_text="character this details belongs to",
    )

    # character public info
    alliance = models.ForeignKey(
        EveEntity,
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        blank=True,
        related_name="owner_alliances",
    )
    birthday = models.DateTimeField()
    corporation = models.ForeignKey(
        EveEntity, on_delete=models.CASCADE, related_name="owner_corporations"
    )
    description = models.TextField(default="")
    eve_ancestry = models.ForeignKey(
        EveAncestry, on_delete=models.SET_DEFAULT, default=None, null=True
    )
    eve_bloodline = models.ForeignKey(EveBloodline, on_delete=models.CASCADE)
    eve_faction = models.ForeignKey(
        EveFaction, on_delete=models.SET_DEFAULT, default=None, null=True
    )
    eve_race = models.ForeignKey(EveRace, on_delete=models.CASCADE)
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES)
    name = models.CharField(max_length=100)
    security_status = models.FloatField(default=None, null=True)
    title = models.TextField(default="")

    def __str__(self):
        return str(self.character)

    @property
    def description_plain(self) -> str:
        """returns the description without tags"""
        return eve_xml_to_html(self.description)


class CharacterCorporationHistory(models.Model):
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="corporation_history"
    )
    record_id = models.PositiveIntegerField(db_index=True)
    corporation = models.ForeignKey(EveEntity, on_delete=models.CASCADE)
    is_deleted = models.BooleanField(null=True, default=None, db_index=True)
    start_date = models.DateTimeField(db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["character", "record_id"],
                name="functional_pk_charactercorporationhistory",
            )
        ]

    def __str__(self):
        return str(f"{self.character}-{self.record_id}")


class CharacterMailingList(models.Model):
    """Mailing list of a character"""

    character = models.ForeignKey(
        Character,
        on_delete=models.CASCADE,
        related_name="mailing_lists",
        help_text="character this mailling list belongs to",
    )
    list_id = models.PositiveIntegerField()
    name = models.CharField(max_length=254)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["character", "list_id"],
                name="functional_pk_charactermailinglist",
            )
        ]

    def __str__(self):
        return self.name


class CharacterMailLabel(models.Model):
    """Mail labels of a character"""

    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="mail_labels"
    )
    label_id = models.PositiveIntegerField(db_index=True)
    name = models.CharField(max_length=40)
    color = models.CharField(max_length=16, default="")
    unread_count = models.PositiveIntegerField(default=None, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["character", "label_id"],
                name="functional_pk_charactermaillabel",
            )
        ]

    def __str__(self):
        return self.name


class CharacterMail(models.Model):
    """Mail of a character"""

    character = models.ForeignKey(
        Character,
        on_delete=models.CASCADE,
        related_name="mails",
        help_text="character this mail belongs to",
    )
    mail_id = models.PositiveIntegerField()
    from_entity = models.ForeignKey(
        EveEntity, on_delete=models.CASCADE, null=True, default=None
    )
    from_mailing_list = models.ForeignKey(
        CharacterMailingList,
        on_delete=models.CASCADE,
        null=True,
        default=None,
        blank=True,
    )
    is_read = models.BooleanField(null=True, default=None)
    subject = models.CharField(max_length=255, null=True, default=None)
    body = models.TextField(null=True, default=None)
    timestamp = models.DateTimeField(null=True, default=None)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["character", "mail_id"], name="functional_pk_charactermail"
            )
        ]

    def __str__(self):
        return str(self.mail_id)

    @property
    def body_html(self) -> str:
        """returns the body as html"""
        return eve_xml_to_html(self.body)


class CharacterMailMailLabel(models.Model):
    """Mail label used in a mail"""

    mail = models.ForeignKey(
        CharacterMail, on_delete=models.CASCADE, related_name="labels"
    )
    label = models.ForeignKey(CharacterMailLabel, on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["mail", "label"],
                name="functional_pk_charactermailmaillabel",
            )
        ]

    def __str__(self):
        return "{}-{}".format(self.mail, self.label)


class CharacterMailRecipient(models.Model):
    """Mail recipient used in a mail"""

    mail = models.ForeignKey(
        CharacterMail, on_delete=models.CASCADE, related_name="recipients"
    )
    eve_entity = models.ForeignKey(
        EveEntity, on_delete=models.CASCADE, default=None, null=True
    )
    mailing_list = models.ForeignKey(
        CharacterMailingList,
        on_delete=models.CASCADE,
        default=None,
        null=True,
        blank=True,
    )

    def __str__(self):
        return self.mailing_list.name if self.mailing_list else self.eve_entity.name


class CharacterSkill(models.Model):
    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="skills"
    )
    eve_type = models.ForeignKey(EveType, on_delete=models.CASCADE)
    active_skill_level = models.PositiveIntegerField()
    skillpoints_in_skill = models.BigIntegerField(validators=[MinValueValidator(0)])
    trained_skill_level = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["character", "eve_type"], name="functional_pk_characterskill"
            )
        ]

    def __str__(self):
        return f"{self.character}-{self.eve_type.name}"


class CharacterWalletJournalEntry(models.Model):
    CONTEXT_ID_TYPE_UNDEFINED = "NON"
    CONTEXT_ID_TYPE_STRUCTURE_ID = "STR"
    CONTEXT_ID_TYPE_STATION_ID = "STA"
    CONTEXT_ID_TYPE_MARKET_TRANSACTION_ID = "MTR"
    CONTEXT_ID_TYPE_CHARACTER_ID = "CHR"
    CONTEXT_ID_TYPE_CORPORATION_ID = "COR"
    CONTEXT_ID_TYPE_ALLIANCE_ID = "ALL"
    CONTEXT_ID_TYPE_EVE_SYSTEM = "EVE"
    CONTEXT_ID_TYPE_INDUSTRY_JOB_ID = "INJ"
    CONTEXT_ID_TYPE_CONTRACT_ID = "CNT"
    CONTEXT_ID_TYPE_PLANET_ID = "PLN"
    CONTEXT_ID_TYPE_SYSTEM_ID = "SYS"
    CONTEXT_ID_TYPE_TYPE_ID = "TYP"
    CONTEXT_ID_CHOICES = (
        (CONTEXT_ID_TYPE_UNDEFINED, "undefined"),
        (CONTEXT_ID_TYPE_STATION_ID, "station_id"),
        (CONTEXT_ID_TYPE_MARKET_TRANSACTION_ID, "market_transaction_id"),
        (CONTEXT_ID_TYPE_CHARACTER_ID, "character_id"),
        (CONTEXT_ID_TYPE_CORPORATION_ID, "corporation_id"),
        (CONTEXT_ID_TYPE_ALLIANCE_ID, "alliance_id"),
        (CONTEXT_ID_TYPE_EVE_SYSTEM, "eve_system"),
        (CONTEXT_ID_TYPE_INDUSTRY_JOB_ID, "industry_job_id"),
        (CONTEXT_ID_TYPE_CONTRACT_ID, "contract_id"),
        (CONTEXT_ID_TYPE_PLANET_ID, "planet_id"),
        (CONTEXT_ID_TYPE_SYSTEM_ID, "system_id"),
        (CONTEXT_ID_TYPE_TYPE_ID, "type_id "),
    )

    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="wallet_journal"
    )
    entry_id = models.BigIntegerField(validators=[MinValueValidator(0)])
    amount = models.DecimalField(
        max_digits=CURRENCY_MAX_DIGITS,
        decimal_places=2,
        default=None,
        null=True,
        blank=True,
    )
    balance = models.DecimalField(
        max_digits=CURRENCY_MAX_DIGITS,
        decimal_places=2,
        default=None,
        null=True,
        blank=True,
    )
    context_id = models.BigIntegerField(default=None, null=True)
    context_id_type = models.CharField(max_length=3, choices=CONTEXT_ID_CHOICES)
    date = models.DateTimeField()
    description = models.TextField()
    first_party = models.ForeignKey(
        EveEntity,
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        blank=True,
        related_name="wallet_journal_entry_first_party_set",
    )
    reason = models.TextField(default="")
    ref_type = models.CharField(max_length=32)
    second_party = models.ForeignKey(
        EveEntity,
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        blank=True,
        related_name="wallet_journal_entry_second_party_set",
    )
    tax = models.DecimalField(
        max_digits=CURRENCY_MAX_DIGITS,
        decimal_places=2,
        default=None,
        null=True,
        blank=True,
    )
    tax_receiver = models.ForeignKey(
        EveEntity,
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        blank=True,
        related_name="wallet_journal_entry_tax_receiver_set",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["character", "entry_id"],
                name="functional_pk_characterwalletjournalentry",
            )
        ]

    def __str__(self):
        return str(self.character) + " " + str(self.entry_id)

    @classmethod
    def match_context_type_id(cls, query: str) -> str:
        if query is not None:
            for id_type, id_type_value in cls.CONTEXT_ID_CHOICES:
                if id_type_value == query:
                    return id_type

        return cls.CONTEXT_ID_TYPE_UNDEFINED
