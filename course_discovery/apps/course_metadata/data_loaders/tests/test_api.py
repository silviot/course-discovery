import datetime
from decimal import Decimal

import ddt
import mock
import responses
from django.test import TestCase
from pytz import UTC

from course_discovery.apps.core.tests.utils import mock_api_callback
from course_discovery.apps.course_metadata.data_loaders.api import (
    OrganizationsApiDataLoader, CoursesApiDataLoader, EcommerceApiDataLoader, AbstractDataLoader, ProgramsApiDataLoader
)
from course_discovery.apps.course_metadata.data_loaders.tests import JSON
from course_discovery.apps.course_metadata.data_loaders.tests.mixins import ApiClientTestMixin, DataLoaderTestMixin
from course_discovery.apps.course_metadata.models import (
    Course, CourseRun, Image, Organization, Seat, Program
)
from course_discovery.apps.course_metadata.tests import mock_data
from course_discovery.apps.course_metadata.tests.factories import (
    CourseRunFactory, SeatFactory, ImageFactory, PersonFactory, VideoFactory
)

LOGGER_PATH = 'course_discovery.apps.course_metadata.data_loaders.api.logger'


class AbstractDataLoaderTest(TestCase):
    def test_clean_string(self):
        """ Verify the method leading and trailing spaces, and returns None for empty strings. """
        # Do nothing for non-string input
        self.assertIsNone(AbstractDataLoader.clean_string(None))
        self.assertEqual(AbstractDataLoader.clean_string(3.14), 3.14)

        # Return None for empty strings
        self.assertIsNone(AbstractDataLoader.clean_string(''))
        self.assertIsNone(AbstractDataLoader.clean_string('    '))
        self.assertIsNone(AbstractDataLoader.clean_string('\t'))

        # Return the stripped value for non-empty strings
        for s in ('\tabc', 'abc', ' abc ', 'abc ', '\tabc\t '):
            self.assertEqual(AbstractDataLoader.clean_string(s), 'abc')

    def test_parse_date(self):
        """ Verify the method properly parses dates. """
        # Do nothing for empty values
        self.assertIsNone(AbstractDataLoader.parse_date(''))
        self.assertIsNone(AbstractDataLoader.parse_date(None))

        # Parse datetime strings
        dt = datetime.datetime.utcnow()
        self.assertEqual(AbstractDataLoader.parse_date(dt.isoformat()), dt)

    def test_delete_orphans(self):
        """ Verify the delete_orphans method deletes orphaned instances. """
        instances = (ImageFactory(), PersonFactory(), VideoFactory(),)
        AbstractDataLoader.delete_orphans()

        for instance in instances:
            self.assertFalse(instance.__class__.objects.filter(pk=instance.pk).exists())  # pylint: disable=no-member


@ddt.ddt
class OrganizationsApiDataLoaderTests(ApiClientTestMixin, DataLoaderTestMixin, TestCase):
    loader_class = OrganizationsApiDataLoader

    @property
    def api_url(self):
        return self.partner.organizations_api_url

    def mock_api(self):
        bodies = mock_data.ORGANIZATIONS_API_BODIES
        url = self.api_url + 'organizations/'
        responses.add_callback(
            responses.GET,
            url,
            callback=mock_api_callback(url, bodies),
            content_type=JSON
        )
        return bodies

    def assert_organization_loaded(self, body):
        """ Assert an Organization corresponding to the specified data body was properly loaded into the database. """
        organization = Organization.objects.get(key=AbstractDataLoader.clean_string(body['short_name']))
        self.assertEqual(organization.name, AbstractDataLoader.clean_string(body['name']))
        self.assertEqual(organization.description, AbstractDataLoader.clean_string(body['description']))

        image = None
        image_url = AbstractDataLoader.clean_string(body['logo'])
        if image_url:
            image = Image.objects.get(src=image_url)

        self.assertEqual(organization.logo_image, image)

    @responses.activate
    def test_ingest(self):
        """ Verify the method ingests data from the Organizations API. """
        api_data = self.mock_api()
        self.assertEqual(Organization.objects.count(), 0)

        self.loader.ingest()

        # Verify the API was called with the correct authorization header
        self.assert_api_called(1)

        # Verify the Organizations were created correctly
        expected_num_orgs = len(api_data)
        self.assertEqual(Organization.objects.count(), expected_num_orgs)

        for datum in api_data:
            self.assert_organization_loaded(datum)

        # Verify multiple calls to ingest data do NOT result in data integrity errors.
        self.loader.ingest()


@ddt.ddt
class CoursesApiDataLoaderTests(ApiClientTestMixin, DataLoaderTestMixin, TestCase):
    loader_class = CoursesApiDataLoader

    @property
    def api_url(self):
        return self.partner.courses_api_url

    def mock_api(self):
        bodies = mock_data.COURSES_API_BODIES
        url = self.api_url + 'courses/'
        responses.add_callback(
            responses.GET,
            url,
            callback=mock_api_callback(url, bodies, pagination=True),
            content_type=JSON
        )
        return bodies

    def assert_course_run_loaded(self, body):
        """ Assert a CourseRun corresponding to the specified data body was properly loaded into the database. """

        # Validate the Course
        course_key = '{org}+{key}'.format(org=body['org'], key=body['number'])
        organization = Organization.objects.get(key=body['org'])
        course = Course.objects.get(key=course_key)

        self.assertEqual(course.title, body['name'])
        self.assertListEqual(list(course.organizations.all()), [organization])

        # Validate the course run
        course_run = CourseRun.objects.get(key=body['id'])
        self.assertEqual(course_run.course, course)
        self.assertEqual(course_run.title, AbstractDataLoader.clean_string(body['name']))
        self.assertEqual(course_run.short_description, AbstractDataLoader.clean_string(body['short_description']))
        self.assertEqual(course_run.start, AbstractDataLoader.parse_date(body['start']))
        self.assertEqual(course_run.end, AbstractDataLoader.parse_date(body['end']))
        self.assertEqual(course_run.enrollment_start, AbstractDataLoader.parse_date(body['enrollment_start']))
        self.assertEqual(course_run.enrollment_end, AbstractDataLoader.parse_date(body['enrollment_end']))
        self.assertEqual(course_run.pacing_type, self.loader.get_pacing_type(body))
        self.assertEqual(course_run.video, self.loader.get_courserun_video(body))

    @responses.activate
    def test_ingest(self):
        """ Verify the method ingests data from the Courses API. """
        api_data = self.mock_api()
        self.assertEqual(Course.objects.count(), 0)
        self.assertEqual(CourseRun.objects.count(), 0)

        self.loader.ingest()

        # Verify the API was called with the correct authorization header
        self.assert_api_called(1)

        # Verify the CourseRuns were created correctly
        expected_num_course_runs = len(api_data)
        self.assertEqual(CourseRun.objects.count(), expected_num_course_runs)

        for datum in api_data:
            self.assert_course_run_loaded(datum)

        # Verify multiple calls to ingest data do NOT result in data integrity errors.
        self.loader.ingest()

    @responses.activate
    def test_ingest_exception_handling(self):
        """ Verify the data loader properly handles exceptions during processing of the data from the API. """
        api_data = self.mock_api()

        with mock.patch.object(self.loader, 'clean_strings', side_effect=Exception):
            with mock.patch(LOGGER_PATH) as mock_logger:
                self.loader.ingest()
                self.assertEqual(mock_logger.exception.call_count, len(api_data))
                msg = 'An error occurred while updating {0} from {1}'.format(
                    api_data[-1]['id'],
                    self.partner.courses_api_url
                )
                mock_logger.exception.assert_called_with(msg)

    def test_get_pacing_type_field_missing(self):
        """ Verify the method returns None if the API response does not include a pacing field. """
        self.assertIsNone(self.loader.get_pacing_type({}))

    @ddt.unpack
    @ddt.data(
        ('', None),
        ('foo', None),
        (None, None),
        ('instructor', CourseRun.INSTRUCTOR_PACED),
        ('Instructor', CourseRun.INSTRUCTOR_PACED),
        ('self', CourseRun.SELF_PACED),
        ('Self', CourseRun.SELF_PACED),
    )
    def test_get_pacing_type(self, pacing, expected_pacing_type):
        """ Verify the method returns a pacing type corresponding to the API response's pacing field. """
        self.assertEqual(self.loader.get_pacing_type({'pacing': pacing}), expected_pacing_type)

    @ddt.unpack
    @ddt.data(
        (None, None),
        ('http://example.com/image.mp4', 'http://example.com/image.mp4'),
    )
    def test_get_courserun_video(self, uri, expected_video_src):
        """ Verify the method returns an Video object with the correct URL. """
        body = {
            'media': {
                'course_video': {
                    'uri': uri
                }
            }
        }
        actual = self.loader.get_courserun_video(body)

        if expected_video_src:
            self.assertEqual(actual.src, expected_video_src)
        else:
            self.assertIsNone(actual)


@ddt.ddt
class EcommerceApiDataLoaderTests(ApiClientTestMixin, DataLoaderTestMixin, TestCase):
    loader_class = EcommerceApiDataLoader

    @property
    def api_url(self):
        return self.partner.ecommerce_api_url

    def mock_api(self):
        # Create existing seats to be removed by ingest
        audit_run = CourseRunFactory(title_override='audit', key='audit/course/run')
        verified_run = CourseRunFactory(title_override='verified', key='verified/course/run')
        credit_run = CourseRunFactory(title_override='credit', key='credit/course/run')
        no_currency_run = CourseRunFactory(title_override='no currency', key='nocurrency/course/run')

        SeatFactory(course_run=audit_run, type=Seat.PROFESSIONAL)
        SeatFactory(course_run=verified_run, type=Seat.PROFESSIONAL)
        SeatFactory(course_run=credit_run, type=Seat.PROFESSIONAL)
        SeatFactory(course_run=no_currency_run, type=Seat.PROFESSIONAL)

        bodies = mock_data.ECOMMERCE_API_BODIES
        url = self.api_url + 'courses/'
        responses.add_callback(
            responses.GET,
            url,
            callback=mock_api_callback(url, bodies),
            content_type=JSON
        )
        return bodies

    def assert_seats_loaded(self, body):
        """ Assert a Seat corresponding to the specified data body was properly loaded into the database. """
        course_run = CourseRun.objects.get(key=body['id'])
        products = [p for p in body['products'] if p['structure'] == 'child']
        # Verify that the old seat is removed
        self.assertEqual(course_run.seats.count(), len(products))

        # Validate each seat
        for product in products:
            stock_record = product['stockrecords'][0]
            price_currency = stock_record['price_currency']
            price = Decimal(stock_record['price_excl_tax'])
            certificate_type = Seat.AUDIT
            credit_provider = None
            credit_hours = None
            if product['expires']:
                upgrade_deadline = datetime.datetime.strptime(
                    product['expires'], "%Y-%m-%dT%H:%M:%SZ"
                ).replace(tzinfo=UTC)
            else:
                upgrade_deadline = None

            for att in product['attribute_values']:
                if att['name'] == 'certificate_type':
                    certificate_type = att['value']
                elif att['name'] == 'credit_provider':
                    credit_provider = att['value']
                elif att['name'] == 'credit_hours':
                    credit_hours = att['value']

            seat = course_run.seats.get(type=certificate_type, credit_provider=credit_provider, currency=price_currency)

            self.assertEqual(seat.course_run, course_run)
            self.assertEqual(seat.type, certificate_type)
            self.assertEqual(seat.price, price)
            self.assertEqual(seat.currency.code, price_currency)
            self.assertEqual(seat.credit_provider, credit_provider)
            self.assertEqual(seat.credit_hours, credit_hours)
            self.assertEqual(seat.upgrade_deadline, upgrade_deadline)

    @responses.activate
    def test_ingest(self):
        """ Verify the method ingests data from the E-Commerce API. """
        api_data = self.mock_api()
        loaded_course_run_data = api_data[:-1]
        loaded_seat_data = api_data[:-2]

        self.assertEqual(CourseRun.objects.count(), len(loaded_course_run_data))

        # Verify a seat exists on all courses already
        for course_run in CourseRun.objects.all():
            self.assertEqual(course_run.seats.count(), 1)

        self.loader.ingest()

        # Verify the API was called with the correct authorization header
        self.assert_api_called(1)

        for datum in loaded_seat_data:
            self.assert_seats_loaded(datum)

        # Verify multiple calls to ingest data do NOT result in data integrity errors.
        self.loader.ingest()

    @ddt.unpack
    @ddt.data(
        ({"attribute_values": []}, Seat.AUDIT),
        ({"attribute_values": [{'name': 'certificate_type', 'value': 'professional'}]}, 'professional'),
        (
            {
                "attribute_values": [
                    {'name': 'other_data', 'value': 'other'},
                    {'name': 'certificate_type', 'value': 'credit'}
                ]
            },
            'credit'
        ),
        ({"attribute_values": [{'name': 'other_data', 'value': 'other'}]}, Seat.AUDIT),
    )
    def test_get_certificate_type(self, product, expected_certificate_type):
        """ Verify the method returns the correct certificate type"""
        self.assertEqual(self.loader.get_certificate_type(product), expected_certificate_type)


@ddt.ddt
class ProgramsApiDataLoaderTests(ApiClientTestMixin, DataLoaderTestMixin, TestCase):
    loader_class = ProgramsApiDataLoader

    @property
    def api_url(self):
        return self.partner.programs_api_url

    def mock_api(self):
        bodies = mock_data.PROGRAMS_API_BODIES
        url = self.api_url + 'programs/'
        responses.add_callback(
            responses.GET,
            url,
            callback=mock_api_callback(url, bodies),
            content_type=JSON
        )

        # We exclude the one invalid item
        return bodies[:-1]

    def assert_program_loaded(self, body):
        """ Assert a Program corresponding to the specified data body was properly loaded into the database. """
        program = Program.objects.get(uuid=AbstractDataLoader.clean_string(body['uuid']))

        self.assertEqual(program.title, body['name'])
        for attr in ('subtitle', 'category', 'status', 'marketing_slug',):
            self.assertEqual(getattr(program, attr), AbstractDataLoader.clean_string(body[attr]))

        keys = [org['key'] for org in body['organizations']]
        expected_organizations = list(Organization.objects.filter(key__in=keys))
        self.assertEqual(keys, [org.key for org in expected_organizations])
        self.assertListEqual(list(program.authoring_organizations.all()), expected_organizations)

        banner_image_url = body.get('banner_image_urls', {}).get('w435h145')
        self.assertEqual(program.banner_image_url, banner_image_url)

    @responses.activate
    def test_ingest(self):
        """ Verify the method ingests data from the Organizations API. """
        api_data = self.mock_api()
        self.assertEqual(Program.objects.count(), 0)

        self.loader.ingest()

        # Verify the API was called with the correct authorization header
        self.assert_api_called(1)

        # Verify the Programs were created correctly
        self.assertEqual(Program.objects.count(), len(api_data))

        for datum in api_data:
            self.assert_program_loaded(datum)

        self.loader.ingest()