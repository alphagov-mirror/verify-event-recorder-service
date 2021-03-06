import os
import urllib.parse
import uuid
from unittest import TestCase

import boto3
import dateparser
import psycopg2
from moto import mock_s3, mock_kms
from parameterized import parameterized
from retrying import retry
from testfixtures import LogCapture

from src import idp_fraud_data_handler, database, event_mapper
from src.database import RunInTransaction
from src.idp_fraud_event import IdpFraudEvent
from test.helpers import IDP_ENTITY_ID, clean_db, file_exists_in_s3, setup_stub_aws_config, \
    DB_PASSWORD

UPLOAD_BUCKET_NAME = 's3-idp-fraud-data-bucket'
UPLOAD_FILE_NAME = 'idp-data.csv'
UPLOAD_USERNAME = 'my.user.name@example.com'


@mock_s3
@mock_kms
class IdpFraudDataHandlerTest(TestCase):
    __s3_client = None
    __kms_client = None
    __queue_url = None
    __key_id = None
    db_connection = None
    db_connection_string = "host='event-store' dbname='events' user='postgres'"

    @classmethod
    def setUpClass(cls):
        cls.connect()

    @classmethod
    @retry(stop_max_attempt_number=5, wait_fixed=500)
    def connect(cls):
        cls.db_connection = psycopg2.connect(cls.db_connection_string)

    def setUp(self):
        setup_stub_aws_config()
        self.__setup_s3()
        self.__setup_db_connection_string()

    def tearDown(self):
        clean_db(self.db_connection)

    def test_writes_messages_to_db(self):
        idp_fraud_events = self.__generate_test_idp_fraud_events()

        self.__write_import_file_to_s3(idp_fraud_events)

        with LogCapture('idp_fraud_data_handler', propagate=False) as log_capture:
            idp_fraud_data_handler.idp_fraud_data_events(self.__create_s3_event(), None)

            log_capture.check(
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Created connection to DB'
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Processing data for IDP {}'.format(IDP_ENTITY_ID)
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Successfully wrote IDP fraud event ID {} to database.'.format(
                        idp_fraud_events[0].idp_event_id
                    )
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Successfully wrote IDP fraud event ID {} to database.'.format(
                        idp_fraud_events[1].idp_event_id
                    )
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Successfully wrote IDP fraud event ID {} to database.'.format(
                        idp_fraud_events[2].idp_event_id
                    )
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Successfully wrote IDP fraud event ID {} to database.'.format(
                        idp_fraud_events[3].idp_event_id
                    )
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Processing successful'
                )
            )
            self.__assert_upload_session_exists_in_database(True)
            self.__assert_events_exist_in_database(idp_fraud_events)
            self.__assert_upload_file_has_been_moved_to_folder(idp_fraud_data_handler.SUCCESS_FOLDER)

    def test_writes_messages_to_db_and_increments_contra_indicator_count_correctly(self):
        idp_fraud_events = [
            IdpFraudEvent(
                timestamp="05/08/2019 11:54",
                idp_event_id="1111111",
                idp_entity_id=IDP_ENTITY_ID,
                fid_code="DF01",
                contra_indicators=["A04", "D02"],
                contra_score=-5,
                request_id="_{}".format(uuid.uuid4()),
                client_ip_address="111.222.222.111",
                pid=str(uuid.uuid4())
            ),
            IdpFraudEvent(
                timestamp="07/08/2019 16:37",
                idp_event_id="2222222",
                idp_entity_id=IDP_ENTITY_ID,
                fid_code="DF01",
                contra_indicators=["A01", "D15", "A01"],
                contra_score=-5,
                request_id="_{}".format(uuid.uuid4()),
                client_ip_address="222.111.111.222",
                pid=str(uuid.uuid4())
            ),
            IdpFraudEvent(
                timestamp="10/08/2019 09:24",
                idp_event_id="3333333",
                idp_entity_id=IDP_ENTITY_ID,
                fid_code="DF01",
                contra_indicators=["A01", "A05", "V03", "A05", "A05", "A05"],
                contra_score=-10,
                request_id="_{}".format(uuid.uuid4()),
                client_ip_address="111.111.111.111",
                pid=str(uuid.uuid4())
            ),
        ]

        self.__write_import_file_to_s3(idp_fraud_events)

        with LogCapture('idp_fraud_data_handler', propagate=False) as log_capture:
            idp_fraud_data_handler.idp_fraud_data_events(self.__create_s3_event(), None)

            log_capture.check(
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Created connection to DB'
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Processing data for IDP {}'.format(IDP_ENTITY_ID)
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Successfully wrote IDP fraud event ID {} to database.'.format(
                        idp_fraud_events[0].idp_event_id
                    )
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Successfully wrote IDP fraud event ID {} to database.'.format(
                        idp_fraud_events[1].idp_event_id
                    )
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Successfully wrote IDP fraud event ID {} to database.'.format(
                        idp_fraud_events[2].idp_event_id
                    )
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Processing successful'
                )
            )
            self.__assert_upload_session_exists_in_database(True)
            self.__assert_events_exist_in_database(idp_fraud_events)
            self.__assert_upload_file_has_been_moved_to_folder(idp_fraud_data_handler.SUCCESS_FOLDER)

    def test_invalid_data_causes_error(self):
        idp_fraud_events = self.__generate_test_idp_fraud_events()
        self.__write_import_file_to_s3(idp_fraud_events, error_rows=[
            '"01/01/2019 11:00",,,'
        ])

        with LogCapture('idp_fraud_data_handler', propagate=False) as log_capture:
            idp_fraud_data_handler.idp_fraud_data_events(self.__create_s3_event(), None)

            log_capture.check(
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Created connection to DB'
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Processing data for IDP {}'.format(IDP_ENTITY_ID)
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Successfully wrote IDP fraud event ID {} to database.'.format(
                        idp_fraud_events[0].idp_event_id
                    )
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Successfully wrote IDP fraud event ID {} to database.'.format(
                        idp_fraud_events[1].idp_event_id
                    )
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Successfully wrote IDP fraud event ID {} to database.'.format(
                        idp_fraud_events[2].idp_event_id
                    )
                ),
                (
                    'idp_fraud_data_handler',
                    'INFO',
                    'Successfully wrote IDP fraud event ID {} to database.'.format(
                        idp_fraud_events[3].idp_event_id
                    )
                ),
                (
                    'idp_fraud_data_handler',
                    'ERROR',
                    'Failed to store IDP fraud event: list index out of range (line 6)'
                ),
                (
                    'idp_fraud_data_handler',
                    'WARNING',
                    'Processing Failed'
                ),
            )
            self.__assert_upload_session_exists_in_database(False)
            self.__assert_no_events_exist_in_database(idp_fraud_events)
            self.__assert_error_in_database_failure_table(
                6,
                '**Row Exception**',
                'Failed to store IDP fraud event: list index out of range (line 6)'
            )
            self.__assert_upload_file_has_been_moved_to_folder(idp_fraud_data_handler.ERROR_FOLDER)

    def test_handles_dates_in_different_formats_and_dst(self):
        idp_fraud_events = self.__generate_test_idp_fraud_events([
            IdpFraudEvent(
                timestamp="2019-03-01T02:40:40.1110000Z",
                idp_event_id="5555555",
                idp_entity_id=IDP_ENTITY_ID,
                fid_code="DF01",
                contra_indicators=["A04", "D02"],
                contra_score=-5,
                request_id="_{}".format(uuid.uuid4()),
                client_ip_address="111.222.222.111",
                pid=str(uuid.uuid4())
            ),
            IdpFraudEvent(
                timestamp="2019-06-01T03:30:30.2220000Z",
                idp_event_id="6666666",
                idp_entity_id=IDP_ENTITY_ID,
                fid_code="DF01",
                contra_indicators=["D15"],
                contra_score=-5,
                request_id="_{}".format(uuid.uuid4()),
                client_ip_address="222.111.111.222",
                pid=str(uuid.uuid4())
            ),
        ])

        self.__write_import_file_to_s3(idp_fraud_events)

        with LogCapture('idp_fraud_data_handler', propagate=False):
            idp_fraud_data_handler.idp_fraud_data_events(self.__create_s3_event(), None)

            self.__assert_upload_session_exists_in_database(True)
            self.__assert_events_exist_in_database(idp_fraud_events)
            self.__assert_upload_file_has_been_moved_to_folder(idp_fraud_data_handler.SUCCESS_FOLDER)

    def test_handles_empty_contra_indicators_and_scores(self):
        idp_fraud_events = self.__generate_test_idp_fraud_events()
        self.__write_import_file_to_s3(idp_fraud_events, ",", [
            '2019-01-20T18:30:15.1110000Z,5555555,DF01,,,_req5555555,111.111.111.111,pid5555555',
            '2019-01-31T16:26:03.2220000Z,6666666,IT01,  ,  ,_req6666666,222.222.222.222,pid6666666',
        ])
        additional_events = [
            IdpFraudEvent(
                timestamp="2019-01-20T18:30:15.1110000Z",
                idp_event_id="5555555",
                idp_entity_id=IDP_ENTITY_ID,
                fid_code="DF01",
                contra_indicators=[],
                contra_score=0,
                request_id="_req5555555",
                client_ip_address="111.111.111.111",
                pid="pid5555555"
            ),
            IdpFraudEvent(
                timestamp="2019-01-31T16:26:03.2220000Z",
                idp_event_id="6666666",
                idp_entity_id=IDP_ENTITY_ID,
                fid_code="IT01",
                contra_indicators=[],
                contra_score=0,
                request_id="_req6666666",
                client_ip_address="222.222.222.222",
                pid="pid6666666"
            )
        ]
        with LogCapture('idp_fraud_data_handler', propagate=False):
            idp_fraud_data_handler.idp_fraud_data_events(self.__create_s3_event(), None)

            self.__assert_upload_session_exists_in_database(True)
            self.__assert_events_exist_in_database(idp_fraud_events + additional_events)
            self.__assert_upload_file_has_been_moved_to_folder(idp_fraud_data_handler.SUCCESS_FOLDER)

    @parameterized.expand([
        ["comma", ","],
        ["lf", "\n"],
        ["crlf", "\r\n"],
    ])
    def test_different_delimiters_in_contra_indicator_list(self, name, delimiter):
        idp_fraud_events = self.__generate_test_idp_fraud_events()

        self.__write_import_file_to_s3(idp_fraud_events, delimiter)

        with LogCapture('idp_fraud_data_handler', propagate=False):
            idp_fraud_data_handler.idp_fraud_data_events(self.__create_s3_event(), None)

            self.__assert_upload_session_exists_in_database(True)
            self.__assert_events_exist_in_database(idp_fraud_events)
            self.__assert_upload_file_has_been_moved_to_folder(idp_fraud_data_handler.SUCCESS_FOLDER)

    def __assert_upload_file_has_been_moved_to_folder(self, folder):
        self.assertFalse(file_exists_in_s3(UPLOAD_BUCKET_NAME, UPLOAD_FILE_NAME))
        self.assertTrue(file_exists_in_s3(
            UPLOAD_BUCKET_NAME,
            '{}/{}'.format(folder, os.path.basename(UPLOAD_FILE_NAME))
        ))

    def __assert_upload_session_exists_in_database(self, passed_validation):
        with RunInTransaction(self.db_connection) as cursor:
            cursor.execute("""
                SELECT
                    id,
                    source_file_name,
                    idp_entity_id,
                    userid,
                    passed_validation
                  FROM idp_data.upload_sessions
            """)
            result = cursor.fetchone()

            self.assertIsNotNone(result)
            self.assertIsNotNone(result[0])
            self.assertEqual(result[1], UPLOAD_FILE_NAME)
            self.assertEqual(result[2], IDP_ENTITY_ID)
            self.assertEqual(result[3], UPLOAD_USERNAME)
            self.assertEqual(result[4], passed_validation)

    def __assert_error_in_database_failure_table(self, row, field, message):
        with RunInTransaction(self.db_connection) as cursor:
            cursor.execute("""
                SELECT
                    id,
                    upload_session_id,
                    row,
                    field,
                    message
                  FROM idp_data.upload_session_validation_failures
            """)
            result = cursor.fetchone()

            self.assertIsNotNone(result)
            self.assertIsNotNone(result[0])
            self.assertIsNotNone(result[1])
            self.assertEqual(result[2], row)
            self.assertEqual(result[3], field)
            self.assertEqual(result[4], message)

    def __assert_events_exist_in_database(self, idp_fraud_events):
        with RunInTransaction(self.db_connection) as cursor:
            for event in idp_fraud_events:
                cursor.execute("""
                    SELECT
                        a.id,
                        a.idp_entity_id,
                        a.idp_event_id,
                        a.time_stamp,
                        a.fid_code,
                        a.request_id,
                        a.pid,
                        a.client_ip_address,
                        a.contra_score,
                        a.upload_session_id,
                        b.source_file_name
                      FROM idp_data.idp_fraud_events a
                     INNER JOIN idp_data.upload_sessions b ON a.upload_session_id = b.id
                     WHERE a.idp_entity_id = %s
                       AND a.idp_event_id = %s
                """, [event.idp_entity_id, event.idp_event_id])
                result = cursor.fetchone()

                self.assertIsNotNone(result)
                self.assertIsNotNone(result[0])
                self.assertEqual(
                    result[3].timestamp(),
                    dateparser.parse(event.timestamp,
                                     settings={'TIMEZONE': idp_fraud_data_handler.DEFAULT_TIMEZONE}).timestamp()
                )
                self.assertEqual(result[4], event.fid_code)
                self.assertEqual(result[5], event.request_id)
                self.assertEqual(result[6], event.pid)
                self.assertEqual(result[7], event.client_ip_address)
                self.assertEqual(result[8], event.contra_score)
                self.assertIsNotNone(result[9])
                self.assertEqual(result[10], UPLOAD_FILE_NAME)

                if event.contra_indicators:
                    contra_indicators = list(set(event.contra_indicators))
                    contra_indicators.sort()
                    cursor.execute("""
                        SELECT
                            contraindicator_code,
                            count
                          FROM
                            idp_data.idp_fraud_event_contraindicators
                         WHERE idp_fraud_events_id = %s
                         ORDER BY contraindicator_code ASC
                    """, [result[0]])

                    for contra_indicator in contra_indicators:
                        contra_result = cursor.fetchone()
                        self.assertEqual(contra_result[0], contra_indicator)
                        expected_count = len([c for c in event.contra_indicators if c == contra_indicator])
                        self.assertEqual(contra_result[1], expected_count)

    def __assert_no_events_exist_in_database(self, idp_fraud_events):
        with RunInTransaction(self.db_connection) as cursor:
            for event in idp_fraud_events:
                cursor.execute("""
                    SELECT
                        a.id,
                        a.idp_entity_id,
                        a.idp_event_id,
                        a.time_stamp,
                        a.fid_code,
                        a.request_id,
                        a.pid,
                        a.client_ip_address,
                        a.contra_score,
                        a.upload_session_id,
                        b.source_file_name
                      FROM idp_data.idp_fraud_events a
                     INNER JOIN idp_data.upload_sessions b ON a.upload_session_id = b.id
                     WHERE a.idp_entity_id = %s
                       AND a.idp_event_id = %s
                """, [event.idp_entity_id, event.idp_event_id])
                result = cursor.fetchone()

                self.assertIsNone(result)

    def __setup_db_connection_string(self):
        os.environ['DB_CONNECTION_STRING'] = "{} password='{}'".format(self.db_connection_string, DB_PASSWORD)

    def __setup_s3(self):
        self.__s3_client = boto3.client('s3')
        self.__s3_client.create_bucket(
            Bucket=UPLOAD_BUCKET_NAME,
        )

    def __write_import_file_to_s3(self, idp_fraud_events, contra_delimiter=',', error_rows=[]):
        rows = [
            'Event Time,Event ID,FID code,Contra Indicators,Contra Score, Request ID, Client IP Address, PID'
        ]
        rows.extend([self.__idp_fraud_event_to_csv_string(event, contra_delimiter) for event in idp_fraud_events])
        rows.extend(error_rows)
        self.__write_to_s3(UPLOAD_BUCKET_NAME, UPLOAD_FILE_NAME, '\n'.join(rows))

    def __write_to_s3(self, bucket_name, filename, content):
        tags = {
            'username': UPLOAD_USERNAME,
            'idp': IDP_ENTITY_ID,
        }
        self.__s3_client.put_object(
            Bucket=bucket_name,
            Key=filename,
            Body=content,
            Tagging=urllib.parse.urlencode(tags)
        )

    def __create_s3_event(self, alternate_file_name=None):
        return {
            "Records": [
                {
                    "s3": {
                        "bucket": {
                            "name": UPLOAD_BUCKET_NAME,
                        },
                        "object": {
                            "key": alternate_file_name if alternate_file_name else UPLOAD_FILE_NAME,
                        }
                    }
                }
            ]
        }

    def __idp_fraud_event_to_csv_string(self, idp_fraud_event, contra_delimiter):
        return '"{}","{}","{}","{}",{},"{}","{}","{}"'.format(
            idp_fraud_event.timestamp,
            idp_fraud_event.idp_event_id,
            idp_fraud_event.fid_code,
            contra_delimiter.join(idp_fraud_event.contra_indicators),
            idp_fraud_event.contra_score,
            idp_fraud_event.request_id,
            idp_fraud_event.client_ip_address,
            idp_fraud_event.pid
        )

    def __generate_test_idp_fraud_events(self, additional_events=[]):
        idp_fraud_events = [
            IdpFraudEvent(
                timestamp="05/08/2019 11:54",
                idp_event_id="1111111",
                idp_entity_id=IDP_ENTITY_ID,
                fid_code="DF01",
                contra_indicators=["A04", "D02"],
                contra_score=-5,
                request_id="_{}".format(uuid.uuid4()),
                client_ip_address="111.222.222.111",
                pid=str(uuid.uuid4())
            ),
            IdpFraudEvent(
                timestamp="07/08/2019 16:37",
                idp_event_id="2222222",
                idp_entity_id=IDP_ENTITY_ID,
                fid_code="DF01",
                contra_indicators=["Z01", "D15"],
                contra_score=-5,
                request_id="_{}".format(uuid.uuid4()),
                client_ip_address="222.111.111.222",
                pid=str(uuid.uuid4())
            ),
            IdpFraudEvent(
                timestamp="10/08/2019 09:24",
                idp_event_id="3333333",
                idp_entity_id=IDP_ENTITY_ID,
                fid_code="DF01",
                contra_indicators=["A01", "A05", "V03"],
                contra_score=-10,
                request_id="_{}".format(uuid.uuid4()),
                client_ip_address="111.111.111.111",
                pid=str(uuid.uuid4())
            ),
            IdpFraudEvent(
                timestamp="23/08/2019 21:22",
                idp_event_id="4444444",
                idp_entity_id=IDP_ENTITY_ID,
                fid_code="DF01",
                contra_indicators=["D02"],
                contra_score=-4,
                request_id="_{}".format(uuid.uuid4()),
                client_ip_address="222.222.222.222",
                pid=str(uuid.uuid4())
            ),
        ]

        idp_fraud_events.extend(additional_events)
        return idp_fraud_events
