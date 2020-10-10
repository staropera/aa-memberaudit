import json
from typing import Optional

from django.contrib.auth.models import User
from django.core.exceptions import ObjectDoesNotExist
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.utils.html import strip_tags
from django.utils.safestring import mark_safe
from django.utils.timezone import now
from django.utils.translation import gettext_lazy as _

from bravado.exception import HTTPError
from esi.models import Token

from eveuniverse.models import (
    EveAncestry,
    EveBloodline,
    EveEntity,
    EveFaction,
    EveRace,
    EveSolarSystem,
    EveType,
)

from allianceauth.eveonline.evelinks import dotlan
from allianceauth.authentication.models import CharacterOwnership
from allianceauth.services.hooks import get_extension_logger

from . import __title__
from .app_settings import MEMBERAUDIT_MAX_MAILS, MEMBERAUDIT_DEVELOPER_MODE
from .decorators import fetch_token
from .managers import CharacterManager, LocationManager
from .providers import esi
from .utils import LoggerAddTag, make_logger_prefix


logger = LoggerAddTag(get_extension_logger(__name__), __title__)

CURRENCY_MAX_DIGITS = 17
CURRENCY_MAX_DECIMALS = 2
NAMES_MAX_LENGTH = 100


def eve_xml_to_html(xml: str) -> str:
    x = xml.replace("<br>", "\n")
    x = strip_tags(x)
    x = x.replace("\n", "<br>")
    return mark_safe(x)


def is_esi_online() -> bool:
    """Returns True if Eve Online server are online, else False"""
    try:
        status = esi.client.Status.get_status().results()
        if status.get("vip"):
            return False

    except HTTPError:
        return False

    return True


def get_or_create_eveuniverse_or_none(
    prop_name: str, dct: dict, Model: type
) -> Optional[models.Model]:
    if dct.get(prop_name):
        obj, _ = Model.objects.get_or_create_esi(id=dct.get(prop_name))
    else:
        obj = None

    return obj


def get_or_create_location_or_none(
    prop_name: str, dct: dict, token: Token
) -> Optional[models.Model]:
    if dct.get(prop_name):
        obj, _ = Location.objects.get_or_create_esi(id=dct.get(prop_name), token=token)
    else:
        obj = None

    return obj


class General(models.Model):
    """Meta model for app permissions"""

    class Meta:
        managed = False
        default_permissions = ()
        permissions = (
            (
                "basic_access",
                "Can access this app and register and view own characters",
            ),
            ("finder_access", "Can access character finder feature"),
            ("reports_access", "Can access reports feature"),
            ("view_shared_characters", "Can view shared characters"),
            ("view_same_corporation", "Can view corporation characters"),
            ("view_same_alliance", "Can view alliance characters"),
            ("view_everything", "Can view all characters"),
        )


class Location(models.Model):
    """An Eve Online location: Station or Upwell Structure"""

    id = models.BigIntegerField(
        primary_key=True,
        validators=[MinValueValidator(0)],
        help_text=(
            "Eve Online location ID, "
            "either item ID for stations or structure ID for structures"
        ),
    )
    name = models.CharField(
        max_length=NAMES_MAX_LENGTH,
        help_text="In-game name of this station or structure",
    )
    eve_solar_system = models.ForeignKey(
        EveSolarSystem,
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        blank=True,
    )
    eve_type = models.ForeignKey(
        EveType,
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        blank=True,
    )
    owner = models.ForeignKey(
        EveEntity,
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        blank=True,
        help_text="corporation this station or structure belongs to",
    )
    updated_at = models.DateTimeField(auto_now=True)

    objects = LocationManager()

    def __str__(self):
        return self.name

    def __repr__(self) -> str:
        return "{}(id={}, name='{}')".format(
            self.__class__.__name__, self.id, self.name
        )

    @property
    def name_plus(self) -> str:
        """return the actual name or 'Unknown location' for empty locations"""
        if self.is_empty:
            return f"Unknown location #{self.id}"

        return self.name

    @property
    def is_empty(self) -> bool:
        return not self.eve_solar_system and not self.eve_type

    @property
    def solar_system_url(self) -> str:
        """returns dotlan URL for this solar system"""
        try:
            return dotlan.solar_system_url(self.eve_solar_system.name)
        except AttributeError:
            return ""


class Character(models.Model):
    """A character synced by this app

    This is the head model for all characters
    """

    UPDATE_SECTION_CHARACTER_DETAILS = "CD"
    UPDATE_SECTION_CONTRACTS = "CR"
    UPDATE_SECTION_CORPORATION_HISTORY = "CH"
    UPDATE_SECTION_JUMP_CLONES = "JC"
    UPDATE_SECTION_MAILS = "MA"
    UPDATE_SECTION_SKILLS = "SK"
    UPDATE_SECTION_WALLET_BALLANCE = "WB"
    UPDATE_SECTION_WALLET_JOURNAL = "WJ"
    UPDATE_SECTION_CHOICES = (
        (UPDATE_SECTION_CHARACTER_DETAILS, _("character details")),
        (UPDATE_SECTION_CONTRACTS, _("contracts")),
        (UPDATE_SECTION_CORPORATION_HISTORY, _("corporation history")),
        (UPDATE_SECTION_JUMP_CLONES, _("jump clones")),
        (UPDATE_SECTION_MAILS, _("mails")),
        (UPDATE_SECTION_SKILLS, _("skills")),
        (UPDATE_SECTION_WALLET_BALLANCE, _("wallet balance")),
        (UPDATE_SECTION_WALLET_JOURNAL, _("wallet journal")),
    )

    character_ownership = models.OneToOneField(
        CharacterOwnership,
        related_name="memberaudit_character",
        on_delete=models.CASCADE,
        primary_key=True,
        help_text="ownership of this character on Auth",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    is_shared = models.BooleanField(
        default=False,
        help_text="Shared characters can be viewed by recruiters",
    )

    objects = CharacterManager()

    def __str__(self):
        return str(self.character_ownership)

    @property
    def has_mails(self):
        return (
            self.mails.count() > 0
            or self.update_status_set.filter(
                section=Character.UPDATE_SECTION_MAILS
            ).exists()
        )

    @property
    def has_wallet_journal(self):
        return (
            self.wallet_journal.count() > 0
            or self.update_status_set.filter(
                section=Character.UPDATE_SECTION_WALLET_JOURNAL
            ).exists()
        )

    def user_has_access(self, user: User) -> bool:
        """Returns True if given user has permission to view this character"""
        if self.character_ownership.user == user:
            return True
        elif user.has_perm("memberaudit.view_everything"):
            return True
        elif (
            user.has_perm("memberaudit.view_same_alliance")
            and user.profile.main_character.alliance_id
            and user.profile.main_character.alliance_id
            == self.character_ownership.character.alliance_id
        ):
            return True
        elif (
            user.has_perm("memberaudit.view_same_corporation")
            and user.profile.main_character.corporation_id
            == self.character_ownership.character.corporation_id
        ):
            return True
        elif user.has_perm("memberaudit.view_shared_characters") and self.is_shared:
            return True

        return False

    def is_update_status_ok(self) -> bool:
        """returns status of last update

        Returns:
        - True: If update was complete and without errors
        - False if there where any errors
        - None: if last update is incomplete
        """
        errors_count = self.update_status_set.filter(is_success=False).count()
        ok_count = self.update_status_set.filter(is_success=True).count()
        if errors_count > 0:
            return False
        elif ok_count == len(Character.UPDATE_SECTION_CHOICES):
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

    @fetch_token("esi-contracts.read_character_contracts.v1")
    def update_contracts(self, token: Token):
        """update the character's contracts"""
        from .tasks import (
            update_contract_details_esi as task_update_contract_details_esi,
        )

        add_prefix = make_logger_prefix(self)
        logger.info(add_prefix("Fetching contracts from ESI"))
        contracts_data = esi.client.Contracts.get_characters_character_id_contracts(
            character_id=self.character_ownership.character.character_id,
            token=token.valid_access_token(),
        ).results()
        availability_map = {
            "alliance": CharacterContract.AVAILABILITY_ALLIANCE,
            "corporation": CharacterContract.AVAILABILITY_CORPORATION,
            "personal": CharacterContract.AVAILABILITY_PERSONAL,
            "public": CharacterContract.AVAILABILITY_PUBLIC,
        }
        status_map = {
            "canceled": CharacterContract.STATUS_CANCELED,
            "deleted": CharacterContract.STATUS_DELETED,
            "failed": CharacterContract.STATUS_FAILED,
            "finished": CharacterContract.STATUS_FINISHED,
            "finished_contractor": CharacterContract.STATUS_FINISHED_CONTRACTOR,
            "finished_issuer": CharacterContract.STATUS_FINISHED_ISSUER,
            "in_progress": CharacterContract.STATUS_IN_PROGRESS,
            "outstanding": CharacterContract.STATUS_OUTSTANDING,
            "rejected": CharacterContract.STATUS_REJECTED,
            "reversed": CharacterContract.STATUS_REVERSED,
        }
        type_map = {
            "auction": CharacterContract.TYPE_AUCTION,
            "courier": CharacterContract.TYPE_COURIER,
            "item_exchange": CharacterContract.TYPE_ITEM_EXCHANGE,
            "loan": CharacterContract.TYPE_LOAN,
            "unknown": CharacterContract.TYPE_UNKNOWN,
        }
        for contract_data in contracts_data:
            contract, _ = CharacterContract.objects.update_or_create(
                character=self,
                contract_id=contract_data.get("contract_id"),
                defaults={
                    "acceptor": get_or_create_eveuniverse_or_none(
                        "acceptor_id", contract_data, EveEntity
                    ),
                    "acceptor_corporation": get_or_create_eveuniverse_or_none(
                        "acceptor_corporation_id", contract_data, EveEntity
                    ),
                    "assignee": get_or_create_eveuniverse_or_none(
                        "assignee_id", contract_data, EveEntity
                    ),
                    "availability": availability_map[contract_data.get("availability")],
                    "buyout": contract_data.get("buyout"),
                    "collateral": contract_data.get("collateral"),
                    "contract_type": type_map[contract_data.get("type")],
                    "date_accepted": contract_data.get("date_accepted"),
                    "date_completed": contract_data.get("date_completed"),
                    "date_expired": contract_data.get("date_expired"),
                    "date_issued": contract_data.get("date_issued"),
                    "days_to_complete": contract_data.get("days_to_complete"),
                    "end_location": get_or_create_location_or_none(
                        "end_location_id", contract_data, token
                    ),
                    "for_corporation": contract_data.get("for_corporation"),
                    "issuer_corporation": get_or_create_eveuniverse_or_none(
                        "issuer_corporation_id", contract_data, EveEntity
                    ),
                    "issuer": get_or_create_eveuniverse_or_none(
                        "issuer_id", contract_data, EveEntity
                    ),
                    "price": contract_data.get("price"),
                    "reward": contract_data.get("reward"),
                    "start_location": get_or_create_location_or_none(
                        "start_location_id", contract_data, token
                    ),
                    "status": status_map[contract_data.get("status")],
                    "title": contract_data.get("title", ""),
                    "volume": contract_data.get("volume"),
                },
            )
            task_update_contract_details_esi(
                character_pk=self.pk, contract_pk=contract.pk
            )

    @fetch_token("esi-contracts.read_character_contracts.v1")
    def update_contract_details(self, token: Token, contract: "CharacterContract"):
        """update the character's contracts"""
        logger.info(
            "%s: Fetching contract details from ESI for ID: %s",
            self,
            contract.contract_id,
        )
        if contract.contract_type in [
            CharacterContract.TYPE_ITEM_EXCHANGE,
            CharacterContract.TYPE_AUCTION,
        ]:
            with transaction.atomic():
                contract.items.all().delete()
                items = esi.client.Contracts.get_characters_character_id_contracts_contract_id_items(
                    character_id=self.character_ownership.character.character_id,
                    contract_id=contract.contract_id,
                    token=token.valid_access_token(),
                ).results()
                for item in items:
                    CharacterContractItem.objects.create(
                        contract=contract,
                        record_id=item.get("record_id"),
                        is_included=item.get("is_included"),
                        is_singleton=item.get("is_singleton"),
                        quantity=item.get("quantity"),
                        raw_quantity=item.get("raw_quantity"),
                        eve_type=get_or_create_eveuniverse_or_none(
                            "type_id", item, EveType
                        ),
                    )

        if contract.contract_type == CharacterContract.TYPE_AUCTION:
            with transaction.atomic():
                contract.bids.all().delete()
                bids = esi.client.Contracts.get_characters_character_id_contracts_contract_id_bids(
                    character_id=self.character_ownership.character.character_id,
                    contract_id=contract.contract_id,
                    token=token.valid_access_token(),
                ).results()
                for bid in bids:
                    CharacterContractBid.objects.create(
                        contract=contract,
                        bid_id=bid.get("bid_id"),
                        amount=bid.get("amount"),
                        bidder=get_or_create_eveuniverse_or_none(
                            "bidder_id", bid, EveEntity
                        ),
                        date_bid=bid.get("date_bid"),
                    )

    @fetch_token("esi-clones.read_clones.v1")
    def update_jump_clones(self, token: Token):
        """updates the character's jump clones"""
        add_prefix = make_logger_prefix(self)
        logger.info(add_prefix("Fetching jump clones from ESI"))
        jump_clones_info = esi.client.Clones.get_characters_character_id_clones(
            character_id=self.character_ownership.character.character_id,
            token=token.valid_access_token(),
        ).results()

        # fetch locations ahead of transaction
        locations = dict()
        for jump_clone_info in jump_clones_info.get("jump_clones", []):
            location_id = jump_clone_info.get("location_id")
            location, _ = Location.objects.get_or_create_esi_async(
                id=location_id, token=token
            )
            locations[location_id] = location

        with transaction.atomic():
            CharacterJumpClone.objects.filter(character=self).delete()
            for jump_clone_info in jump_clones_info.get("jump_clones", []):
                name = (
                    jump_clone_info.get("name") if jump_clone_info.get("name") else ""
                )
                jump_clone = CharacterJumpClone.objects.create(
                    character=self,
                    jump_clone_id=jump_clone_info.get("jump_clone_id"),
                    location=locations[jump_clone_info.get("location_id")],
                    name=name,
                )
                for implant in jump_clone_info.get("implants", []):
                    eve_type, _ = EveType.objects.get_or_create_esi(id=implant)
                    CharacterJumpCloneImplant.objects.create(
                        jump_clone=jump_clone, eve_type=eve_type
                    )

    @fetch_token("esi-mail.read_mail.v1")
    def update_mails(self, token: Token):
        self._update_mailinglists(token)
        self._update_maillabels(token)
        self._update_mail_headers(token)
        self._update_mail_bodies()

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
        CharacterMailUnreadCount.objects.update_or_create(
            character=self,
            defaults={"total": mail_labels_info.get("total_unread_count")},
        )
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

    def _update_mail_headers(self, token: Token):
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
                f"Updating {mail_headers_all} mail headers and loading mail bodies"
            )
        )

        # load mail headers
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

    def _update_mail_bodies(self):
        from .tasks import (
            update_mail_body_esi as task_update_mail_body_esi,
            DEFAULT_TASK_PRIORITY,
        )

        mails_without_body_qs = self.mails.filter(body="")
        for mail in mails_without_body_qs:
            task_update_mail_body_esi.apply_async(
                kwargs={"character_pk": self.pk, "mail_pk": mail.pk},
                priority=DEFAULT_TASK_PRIORITY,
            )

        if mails_without_body_qs.count() > 0:
            logger.info(
                "Character %s: loaded %d mail bodies",
                self.pk,
                mails_without_body_qs.count(),
            )

    @fetch_token("esi-mail.read_mail.v1")
    def update_mail_body(self, token: Token, mail: "CharacterMail") -> None:
        logger.debug(
            "Character %s: Fetching body from ESI for mail ID %s", self.pk, mail.mail_id
        )
        mail_body = esi.client.Mail.get_characters_character_id_mail_mail_id(
            character_id=self.character_ownership.character.character_id,
            mail_id=mail.mail_id,
            token=token.valid_access_token(),
        ).result()
        mail.body = mail_body.get("body", "")
        mail.save()

    @fetch_token("esi-skills.read_skills.v1")
    def update_skills(self, token):
        """syncs the character's skill"""
        add_prefix = make_logger_prefix(self)
        logger.info(add_prefix("Fetching skills from ESI"))

        skills = esi.client.Skills.get_characters_character_id_skills(
            character_id=self.character_ownership.character.character_id,
            token=token.valid_access_token(),
        ).results()
        CharacterSkillpoints.objects.update_or_create(
            character=self,
            defaults={
                "total": skills.get("total_sp"),
                "unallocated": skills.get("unallocated_sp"),
            },
        )
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
        logger.info(add_prefix("Fetching wallet balance from ESI"))
        balance = esi.client.Wallet.get_characters_character_id_wallet(
            character_id=self.character_ownership.character.character_id,
            token=token.valid_access_token(),
        ).results()
        CharacterWalletBalance.objects.update_or_create(
            character=self, defaults={"total": balance}
        )

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
        if not is_esi_online():
            return None, None

        location_info = esi.client.Location.get_characters_character_id_location(
            character_id=self.character_ownership.character.character_id,
            token=token.valid_access_token(),
        ).results()

        solar_system, _ = EveSolarSystem.objects.get_or_create_esi(
            id=location_info.get("solar_system_id")
        )
        if location_info.get("station_id"):
            location, _ = Location.objects.get_or_create_esi(
                id=location_info.get("station_id"), token=token
            )
        elif location_info.get("structure_id"):
            location, _ = Location.objects.get_or_create_esi(
                id=location_info.get("structure_id"), token=token
            )
        else:
            location = None

        return solar_system, location

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

    @classmethod
    def section_method_name(cls, section: str) -> str:
        for short_name, long_name in cls.UPDATE_SECTION_CHOICES:
            if short_name == section:
                method_partial = long_name.replace(" ", "_")
                return f"update_{method_partial}"

        raise ValueError(f"Unknown update section: {section}")


class CharacterContract(models.Model):
    """An Eve Online contract belonging to a Character"""

    AVAILABILITY_ALLIANCE = "AL"
    AVAILABILITY_CORPORATION = "CO"
    AVAILABILITY_PERSONAL = "PR"
    AVAILABILITY_PUBLIC = "PU"
    AVAILABILITY_CHOICES = (
        (AVAILABILITY_ALLIANCE, _("alliance")),
        (AVAILABILITY_CORPORATION, _("corporation")),
        (AVAILABILITY_PERSONAL, _("private")),
        (AVAILABILITY_PUBLIC, _("public")),
    )

    STATUS_OUTSTANDING = "OS"
    STATUS_IN_PROGRESS = "IP"
    STATUS_FINISHED_ISSUER = "FI"
    STATUS_FINISHED_CONTRACTOR = "FC"
    STATUS_FINISHED = "FS"
    STATUS_CANCELED = "CA"
    STATUS_REJECTED = "RJ"
    STATUS_FAILED = "FL"
    STATUS_DELETED = "DL"
    STATUS_REVERSED = "RV"

    STATUS_CHOICES = (
        (STATUS_CANCELED, _("canceled")),
        (STATUS_DELETED, _("deleted")),
        (STATUS_FAILED, _("failed")),
        (STATUS_FINISHED, _("finished")),
        (STATUS_FINISHED_CONTRACTOR, _("finished contractor")),
        (STATUS_FINISHED_ISSUER, _("finished issuer")),
        (STATUS_IN_PROGRESS, _("in progress")),
        (STATUS_OUTSTANDING, _("outstanding")),
        (STATUS_REJECTED, _("rejected")),
        (STATUS_REVERSED, _("reversed")),
    )

    TYPE_AUCTION = "AT"
    TYPE_COURIER = "CR"
    TYPE_ITEM_EXCHANGE = "IE"
    TYPE_LOAN = "LN"
    TYPE_UNKNOWN = "UK"
    TYPE_CHOICES = (
        (TYPE_AUCTION, _("auction")),
        (TYPE_COURIER, _("courier")),
        (TYPE_ITEM_EXCHANGE, _("item exchange")),
        (TYPE_LOAN, _("loan")),
        (TYPE_UNKNOWN, _("unknown")),
    )

    character = models.ForeignKey(
        Character,
        on_delete=models.CASCADE,
        related_name="contracts",
    )
    contract_id = models.IntegerField()

    acceptor = models.ForeignKey(
        EveEntity,
        on_delete=models.CASCADE,
        default=None,
        null=True,
        related_name="acceptor_character_contracts",
        help_text="Who will accept the contract if character",
    )
    acceptor_corporation = models.ForeignKey(
        EveEntity,
        on_delete=models.CASCADE,
        default=None,
        null=True,
        related_name="acceptor_corporation_contracts",
        help_text="corporation of acceptor",
    )
    assignee = models.ForeignKey(
        EveEntity,
        on_delete=models.CASCADE,
        default=None,
        null=True,
        related_name="assignee_character_contracts",
        help_text="To whom the contract is assigned, can be a corporation or a character",
    )
    availability = models.CharField(
        max_length=2,
        choices=AVAILABILITY_CHOICES,
        help_text="To whom the contract is available",
    )
    buyout = models.DecimalField(
        max_digits=CURRENCY_MAX_DIGITS,
        decimal_places=CURRENCY_MAX_DECIMALS,
        default=None,
        null=True,
    )
    collateral = models.DecimalField(
        max_digits=CURRENCY_MAX_DIGITS,
        decimal_places=CURRENCY_MAX_DECIMALS,
        default=None,
        null=True,
    )
    contract_type = models.CharField(max_length=2, choices=TYPE_CHOICES)
    date_accepted = models.DateTimeField(default=None, null=True)
    date_completed = models.DateTimeField(default=None, null=True)
    date_expired = models.DateTimeField()
    date_issued = models.DateTimeField()
    days_to_complete = models.IntegerField(default=None, null=True)
    end_location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name="contract_end_location",
        default=None,
        null=True,
    )
    for_corporation = models.BooleanField()
    issuer_corporation = models.ForeignKey(
        EveEntity,
        on_delete=models.CASCADE,
        related_name="issuer_corporation_contracts",
    )
    issuer = models.ForeignKey(
        EveEntity, on_delete=models.CASCADE, related_name="issuer_character_contracts"
    )
    price = models.DecimalField(
        max_digits=CURRENCY_MAX_DIGITS,
        decimal_places=CURRENCY_MAX_DECIMALS,
        default=None,
        null=True,
    )
    reward = models.DecimalField(
        max_digits=CURRENCY_MAX_DIGITS,
        decimal_places=CURRENCY_MAX_DECIMALS,
        default=None,
        null=True,
    )
    start_location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name="contract_start_location",
        default=None,
        null=True,
    )
    status = models.CharField(max_length=2, choices=STATUS_CHOICES)
    title = models.CharField(max_length=NAMES_MAX_LENGTH, default="")
    volume = models.FloatField(default=None, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["character", "contract_id"],
                name="functional_pk_charactercontract",
            )
        ]

    def __str__(self) -> str:
        return f"{self.character}-{self.contract_id}"

    @property
    def is_completed(self) -> bool:
        """whether this contract is completed or active"""
        return self.status in [
            self.STATUS_FINISHED_ISSUER,
            self.STATUS_FINISHED_CONTRACTOR,
            self.STATUS_FINISHED_ISSUER,
            self.STATUS_CANCELED,
            self.STATUS_REJECTED,
            self.STATUS_DELETED,
            self.STATUS_FINISHED,
            self.STATUS_FAILED,
        ]

    @property
    def is_in_progress(self) -> bool:
        return self.status == self.STATUS_IN_PROGRESS

    @property
    def is_failed(self) -> bool:
        return self.status == self.STATUS_FAILED

    @property
    def has_expired(self) -> bool:
        """returns true if this contract is expired"""
        return self.date_expired < now()

    @property
    def hours_issued_2_completed(self) -> float:
        if self.date_completed:
            td = self.date_completed - self.date_issued
            return td.days * 24 + (td.seconds / 3600)
        else:
            return None

    def summary(self) -> str:
        """return summary text for this contract"""
        if self.contract_type == CharacterContract.TYPE_COURIER:
            summary = (
                f"{self.start_location.eve_solar_system} >> "
                f"{self.end_location.eve_solar_system} "
                f"({self.volume:.0f} m3)"
            )
        else:
            if self.items.filter(is_included=True).count() > 1:
                summary = _("[Multiple Items]")
            else:
                first_item = self.items.first()
                summary = first_item.eve_type.name if first_item else "(no items)"

        return summary


class CharacterContractBid(models.Model):
    contract = models.ForeignKey(
        CharacterContract, on_delete=models.CASCADE, related_name="bids"
    )
    bid_id = models.PositiveIntegerField(db_index=True)

    amount = models.FloatField()
    bidder = models.ForeignKey(EveEntity, on_delete=models.CASCADE)
    date_bid = models.DateTimeField()

    def __str__(self) -> str:
        return f"{self.contract}-{self.bid_id}"


class CharacterContractItem(models.Model):
    contract = models.ForeignKey(
        CharacterContract, on_delete=models.CASCADE, related_name="items"
    )
    record_id = models.PositiveIntegerField(db_index=True)
    is_included = models.BooleanField()
    is_singleton = models.BooleanField()
    quantity = models.PositiveIntegerField()
    raw_quantity = models.IntegerField(default=None, null=True)
    eve_type = models.ForeignKey(EveType, on_delete=models.CASCADE)

    def __str__(self) -> str:
        return f"{self.contract}-{self.record_id}"


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
    description = models.TextField()
    eve_ancestry = models.ForeignKey(
        EveAncestry, on_delete=models.SET_DEFAULT, default=None, null=True
    )
    eve_bloodline = models.ForeignKey(EveBloodline, on_delete=models.CASCADE)
    eve_faction = models.ForeignKey(
        EveFaction, on_delete=models.SET_DEFAULT, default=None, null=True
    )
    eve_race = models.ForeignKey(EveRace, on_delete=models.CASCADE)
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES)
    name = models.CharField(max_length=NAMES_MAX_LENGTH)
    security_status = models.FloatField(default=None, null=True)
    title = models.TextField()

    def __str__(self):
        return str(self.character)

    @property
    def description_plain(self) -> str:
        """returns the description without tags"""
        return eve_xml_to_html(self.description)


class CharacterJumpClone(models.Model):
    """Jump clone of a character"""

    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="jump_clones"
    )
    jump_clone_id = models.PositiveIntegerField()
    location = models.ForeignKey(Location, on_delete=models.CASCADE)
    name = models.CharField(max_length=NAMES_MAX_LENGTH, default="")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["character", "jump_clone_id"],
                name="functional_pk_characterjumpclone",
            )
        ]

    def __str__(self):
        return str(f"{self.character}-{self.jump_clone_id}")


class CharacterJumpCloneImplant(models.Model):
    """Implant of a character jump clone"""

    jump_clone = models.ForeignKey(
        CharacterJumpClone, on_delete=models.CASCADE, related_name="implants"
    )
    eve_type = models.ForeignKey(EveType, on_delete=models.CASCADE)

    def __str__(self):
        return str(f"{self.jump_clone}-{self.eve_type}")


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
        "CharacterMailingList",
        on_delete=models.CASCADE,
        null=True,
        default=None,
        blank=True,
    )
    is_read = models.BooleanField(null=True, default=None)
    subject = models.CharField(max_length=255, default="")
    body = models.TextField()
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


class CharacterMailUnreadCount(models.Model):
    """Wallet balance of a character"""

    character = models.OneToOneField(
        Character,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="unread_mail_count",
    )
    total = models.PositiveIntegerField()


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


class CharacterSkillpoints(models.Model):
    """Skillpoints of a character"""

    character = models.OneToOneField(
        Character,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="skillpoints",
    )
    total = models.BigIntegerField(validators=[MinValueValidator(0)])
    unallocated = models.PositiveIntegerField(default=None, null=True)


class CharacterUpdateStatus(models.Model):
    """Update status for a character"""

    character = models.ForeignKey(
        Character, on_delete=models.CASCADE, related_name="update_status_set"
    )
    section = models.CharField(max_length=2, choices=Character.UPDATE_SECTION_CHOICES)
    is_success = models.BooleanField(db_index=True)
    error_message = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["character", "section"],
                name="functional_pk_charactersyncstatus",
            )
        ]

    def __str__(self):
        return f"{self.character}-{self.get_section_display()}"


class CharacterWalletBalance(models.Model):
    """Wallet balance of a character"""

    character = models.OneToOneField(
        Character,
        primary_key=True,
        on_delete=models.CASCADE,
        related_name="wallet_balance",
    )
    total = models.DecimalField(
        max_digits=CURRENCY_MAX_DIGITS, decimal_places=CURRENCY_MAX_DECIMALS
    )


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
        decimal_places=CURRENCY_MAX_DECIMALS,
        default=None,
        null=True,
        blank=True,
    )
    balance = models.DecimalField(
        max_digits=CURRENCY_MAX_DIGITS,
        decimal_places=CURRENCY_MAX_DECIMALS,
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
    reason = models.TextField()
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
        decimal_places=CURRENCY_MAX_DECIMALS,
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
