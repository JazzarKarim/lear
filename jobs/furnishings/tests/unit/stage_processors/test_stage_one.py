# Copyright © 2024 Province of British Columbia
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the Furnishings Job.

Test suite to ensure that the Furnishings Job stage one is working as expected.
"""
import copy
import datetime
from unittest.mock import MagicMock, patch

import pytest
import requests
from legal_api.models import Address, Business, Furnishing
from legal_api.services.bootstrap import AccountService
from legal_api.services.furnishing_documents_service import FurnishingDocumentsService
from legal_api.utils.datetime import datetime as datetime_util
from registry_schemas.example_data import FILING_HEADER, RESTORATION

from furnishings.stage_processors.stage_one import StageOneProcessor, process

from .. import (
    factory_address,
    factory_batch,
    factory_batch_processing,
    factory_business,
    factory_completed_filing,
    factory_furnishing,
)


RESTORATION_FILING = copy.deepcopy(FILING_HEADER)
RESTORATION_FILING['filing']['restoration'] = RESTORATION


@pytest.mark.parametrize(
    'test_name, mock_return', [
        ('EMAIL', {'contacts': [{'email': 'test@no-reply.com'}]}),
        ('NO_EMAIL', {'contacts': []})
    ]
)
def test_get_email_address_from_auth(session, test_name, mock_return):
    """Assert that email address is returned."""
    token = 'token'
    mock_response = MagicMock()
    mock_response.json.return_value = mock_return
    with patch.object(AccountService, 'get_bearer_token', return_value=token):
        with patch.object(requests, 'get', return_value=mock_response):
            email = StageOneProcessor._get_email_address_from_auth('BC1234567')
            if test_name == 'NO_EMAIL':
                assert email is None
            else:
                assert email == 'test@no-reply.com'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'test_name, entity_type, email, expected_furnishing_name', [
        (
            'BC_AR_OVERDUE',
            Business.LegalTypes.COMP.value,
            'test@no-reply.com',
            Furnishing.FurnishingName.DISSOLUTION_COMMENCEMENT_NO_AR
        ),
        (
            'BC_TRANSITION_OVERDUE',
            Business.LegalTypes.COMP.value,
            'test@no-reply.com',
            Furnishing.FurnishingName.DISSOLUTION_COMMENCEMENT_NO_TR
        ),
        (
            'XP_AR_OVERDUE',
            Business.LegalTypes.EXTRA_PRO_A.value,
            'test@no-reply.com',
            Furnishing.FurnishingName.DISSOLUTION_COMMENCEMENT_NO_AR_XPRO
        ),
        (
            'NO_EMAIL_NO_FURNISHING_ENTRY',
            Business.LegalTypes.COMP.value,
            None,
            Furnishing.FurnishingName.DISSOLUTION_COMMENCEMENT_NO_AR
        )
    ]
)
async def test_process_first_notification(app, session, test_name, entity_type, email, expected_furnishing_name):
    """Assert that the first notification furnishing entry is created correctly."""
    business = factory_business(identifier='BC1234567', entity_type=entity_type)
    mailing_address = factory_address(address_type=Address.MAILING, business_id=business.id)
    batch = factory_batch()
    batch_processing = factory_batch_processing(
        batch_id=batch.id,
        business_id=business.id,
        identifier=business.identifier,
    )
    if 'TRANSITION' in test_name:
        factory_completed_filing(business, RESTORATION_FILING, filing_type='restoration')

    qsm = MagicMock()
    with patch.object(StageOneProcessor, '_get_email_address_from_auth', return_value=email):
        with patch.object(StageOneProcessor, '_send_email', return_value=None) as mock_send_email:
            processor = StageOneProcessor(app, qsm)
            await processor.process(batch_processing)

            if email:
                mock_send_email.assert_called()
                furnishings = Furnishing.find_by(business_id=business.id)
                assert len(furnishings) == 1
                furnishing = furnishings[0]
                assert furnishing.furnishing_type == Furnishing.FurnishingType.EMAIL
                assert furnishing.email == 'test@no-reply.com'
                assert furnishing.furnishing_name == expected_furnishing_name
                assert furnishing.status == Furnishing.FurnishingStatus.QUEUED
                assert furnishing.furnishing_group_id is not None
                assert furnishing.last_ar_date == business.founding_date
                assert furnishing.business_name == business.legal_name
            else:
                mock_send_email.assert_not_called()
                furnishings = Furnishing.find_by(business_id=business.id)
                assert len(furnishings) == 1
                furnishing = furnishings[0]
                assert furnishing.furnishing_type == Furnishing.FurnishingType.MAIL
                assert furnishing.furnishing_name == expected_furnishing_name
                assert furnishing.status == Furnishing.FurnishingStatus.QUEUED
                assert furnishing.furnishing_group_id is not None

                furnishing_addresses = Address.find_by(furnishings_id=furnishing.id)
                assert len(furnishing_addresses) == 1
                furnishing_address = furnishing_addresses[0]
                assert furnishing_address
                assert furnishing_address.address_type == mailing_address.address_type
                assert furnishing_address.furnishings_id == furnishing.id
                assert furnishing_address.business_id == None
                assert furnishing_address.office_id == None

@pytest.mark.asyncio
@pytest.mark.parametrize(
    'test_name, has_email_furnishing, has_mail_furnishing, is_email_elapsed', [
        (
            'EMAIL_FURNISHING_NOT_ELAPSED',
            True,
            False,
            False
        ),
        (
            'HAS_MAIL_FURNISHING',
            True,
            True,
            True
        ),
        (
            'ELIGIBLE_FOR_SECOND_NOTIFICATION',
            True,
            False,
            True
        ),
    ]
)
async def test_process_second_notification(app, session, test_name, has_email_furnishing, has_mail_furnishing, is_email_elapsed):
    """Assert that the second notification furnishing entry is created correctly."""
    business = factory_business(identifier='BC1234567')
    mailing_address = factory_address(address_type=Address.MAILING, business_id=business.id)
    batch = factory_batch()
    batch_processing = factory_batch_processing(
        batch_id=batch.id,
        business_id=business.id,
        identifier=business.identifier,
    )

    existing_furnishings = 0
    if has_email_furnishing:
        email_furnishing = factory_furnishing(
            batch_id=batch.id,
            business_id=business.id,
            identifier=business.identifier,
            furnishing_type=Furnishing.FurnishingType.EMAIL,
            status=Furnishing.FurnishingStatus.QUEUED,
        )
        existing_furnishings += 1

        if is_email_elapsed:
            days_elapsed = int(app.config.get('SECOND_NOTICE_DELAY')) + 1
            email_furnishing.created_date = datetime_util.add_business_days(datetime.datetime.utcnow(), -days_elapsed)
            email_furnishing.save()
    
    if has_mail_furnishing:
        factory_furnishing(
            batch_id=batch.id,
            business_id=business.id,
            identifier=business.identifier,
            furnishing_type=Furnishing.FurnishingType.MAIL,
            status=Furnishing.FurnishingStatus.PROCESSED
        )
        existing_furnishings += 1

    qsm = MagicMock()
    processor = StageOneProcessor(app, qsm)
    await processor.process(batch_processing)

    furnishings = Furnishing.find_by(business_id=business.id)

    if has_email_furnishing and not has_mail_furnishing and is_email_elapsed:
        assert len(furnishings) == 2
        mail_furnishing = next((f for f in furnishings if f.furnishing_type == Furnishing.FurnishingType.MAIL), None)
        assert mail_furnishing
        assert mail_furnishing.status == Furnishing.FurnishingStatus.QUEUED
        assert mail_furnishing.furnishing_group_id is not None

        furnishing_addresses = Address.find_by(furnishings_id=mail_furnishing.id)
        assert len(furnishing_addresses) == 1
        furnishing_address = furnishing_addresses[0]
        assert furnishing_address
        assert furnishing_address.address_type == mailing_address.address_type
        assert furnishing_address.furnishings_id == mail_furnishing.id
        assert furnishing_address.business_id == None
        assert furnishing_address.office_id == None

    else:
        # any other case should not create additional furnishings
        assert len(furnishings) == existing_furnishings


@pytest.mark.asyncio
@pytest.mark.parametrize(
        'test_name, entity_type', [
            ('TEST_FIRST_ROUND_BC', Business.LegalTypes.COMP.value),
            ('TEST_SECOND_ROUND_BC', Business.LegalTypes.COMP.value),
            ('TEST_FIRST_ROUND_XPRO', Business.LegalTypes.EXTRA_PRO_A.value),
            ('TEST_SECOND_ROUND_XPRO', Business.LegalTypes.EXTRA_PRO_A.value),
            ('TEST_NO_GENERATION', Business.LegalTypes.COMP.value)
        ]
)
async def test_generate_paper_letters(app, session, test_name, entity_type):
    """Assert that the merged paper letter is generated correctly."""
    business = factory_business(identifier='BC1234567', entity_type=entity_type)
    factory_address(address_type=Address.MAILING, business_id=business.id)
    batch = factory_batch()
    factory_batch_processing(
        batch_id=batch.id,
        business_id=business.id,
        identifier=business.identifier,
    )

    email = None
    if 'SECOND' in test_name:
        email = 'test@no-reply.com'
        email_furnishing = factory_furnishing(
            batch_id=batch.id,
            business_id=business.id,
            identifier=business.identifier,
            furnishing_type=Furnishing.FurnishingType.EMAIL,
            status=Furnishing.FurnishingStatus.QUEUED,
        )
        days_elapsed = int(app.config.get('SECOND_NOTICE_DELAY')) + 1
        email_furnishing.created_date = datetime_util.add_business_days(datetime.datetime.utcnow(), -days_elapsed)
        email_furnishing.save()
    
    if test_name == 'TEST_NO_GENERATION':
        factory_furnishing(
            batch_id=batch.id,
            business_id=business.id,
            identifier=business.identifier,
            furnishing_type=Furnishing.FurnishingType.MAIL,
            status=Furnishing.FurnishingStatus.PROCESSED,
        )

    qsm = MagicMock()
    with patch.object(StageOneProcessor, '_get_email_address_from_auth', return_value=email):
        with patch.object(FurnishingDocumentsService, 'get_merged_furnishing_document', return_value=b'TEST') as mock_get_document:
            await process(app, qsm)
            if test_name == 'TEST_NO_GENERATION':
                mock_get_document.assert_not_called()
            else:
                mock_get_document.assert_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
        'test_name, entity_type', [
            ('BC_BATCH_LETTER_SFTP', Business.LegalTypes.COMP.value),
            ('XPRO_BATCH_LETTER_SFTP', Business.LegalTypes.EXTRA_PRO_A.value),
        ]
)
async def test_process_paper_letters(app, session, sftpserver, sftpconnection, test_name, entity_type):
    """Assert that SFTP of PDFs is working correctly."""
    business = factory_business(identifier='BC1234567', entity_type=entity_type)
    factory_address(address_type=Address.MAILING, business_id=business.id)
    batch = factory_batch()
    factory_batch_processing(
        batch_id=batch.id,
        business_id=business.id,
        identifier=business.identifier,
    )
    
    mail_furnishing = factory_furnishing(
        batch_id=batch.id,
        business_id=business.id,
        identifier=business.identifier,
        furnishing_type=Furnishing.FurnishingType.MAIL,
        status=Furnishing.FurnishingStatus.QUEUED,
    )

    qsm = MagicMock()
    processor = StageOneProcessor(app, qsm)
    storage_directory = app.config.get('BCMAIL_SFTP_STORAGE_DIRECTORY')
    # Serve content with the specified storage directory
    with sftpserver.serve_content({storage_directory: {}}):
        # Patch the necessary attributes of the processor
        with patch.object(processor, '_bcmail_sftp_connection', new=sftpconnection), \
            patch.object(processor, '_disable_bcmail_sftp', new=False):

            # Determine which furnishings to patch based on the test name
            furnishings_attr = '_bc_mail_furnishings' if test_name == 'BC_BATCH_LETTER_SFTP' else '_xpro_mail_furnishings'

            with patch.object(processor, furnishings_attr, new=[mail_furnishing]):
                processor.process_paper_letters()

                # Assert that a PDF file is uploaded
                with sftpconnection as sftpclient:
                    uploaded_files = sftpclient.listdir(storage_directory)
                    assert len(uploaded_files) == 1

                    # Fetch the updated furnishing
                    updated_furnishing = Furnishing.find_by_id(mail_furnishing.id)

                    # Assert the status is updated correctly
                    assert updated_furnishing.status == Furnishing.FurnishingStatus.PROCESSED

                    # Assert the processed_date is set
                    assert updated_furnishing.processed_date is not None

                    # Assert the correct note is added based on the entity type
                    expected_note = 'SFTP of BC batch letter was a success' if entity_type == Business.LegalTypes.COMP.value else 'SFTP of XPRO batch letter was a success'
                    assert expected_note in updated_furnishing.notes
