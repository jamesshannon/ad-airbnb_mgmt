from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
import os
import re
import shelve
import typing as t

from appdaemon.plugins.hass import Hass # pyright: ignore

# pyright: reportUnknownMemberType=false

# Directory for database files. HACS rewrites the app directory so we keep
# the database in the parent directory.
MY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_BASE = os.path.join(MY_DIR, 'airbnb_state')

class CalendarEventInfo(t.TypedDict):
  """ Data about each Rental Control calendar event
  """
  name: str
  start_date: date
  end_date: date

class RentalEventInfo(t.TypedDict):
  """ Data about Rental Control calendar events relevant to a unit

  String values are the entity name of the calendar event while None means
  that there is no applicable calendar event and thus no applicable event
  """
  checkin_today_evt: str | None
  checkout_today_evt: str | None
  checkin_active_evt: str | None


class AirbnbManagement(Hass):
  def initialize(self):
    """AppDaemon app initialization
    Create/open the database and begin executing the checks
    """
    self.db = shelve.open(f'{ DB_BASE }_{ self.name }')
    self.log(dict(self.db.items()))

    self.check_interval_mins: int = \
        t.cast(int, self.args.get('check_interval_mins', 15))
    self.default_checkin_time: str = \
        t.cast(str, self.args.get('default_checkin_time', '16:00:00'))
    self.checkout_time: time = \
        time.fromisoformat(
            t.cast(str, self.args.get('checkout_time', '11:00:00')))
    self.cleaner_check_time: time = \
        time.fromisoformat(
            t.cast(str, self.args.get('cleaner_check_time', '14:00:00')))


    # Expect all unit parameters as top-level args
    self.unit: dict[str, str] = {
      'name': self.args['name'],
      'code': self.args['code'],
      'cal_code': self.args['cal_code'],
      'thermostat_key': self.args['thermostat_key'],
    }

    # Execute the check every x minutes
    self.run_every(self.check_mgmt, interval=(self.check_interval_mins * 60))

  def terminate(self):
    """ AppDaemon app cleanup

    Close the database
    """
    self.db.close()

  def check_mgmt(self, **kwargs: dict[str, t.Any]):
    self.log('Executing Airbnb Management activities')

    # Get the relevant rental events for this unit
    rental_events = self._get_rental_events(self.unit['cal_code'])

    self.reset_checkin_time()

    if rental_events['checkout_today_evt']:
      self.hvac_off()

    if rental_events['checkin_today_evt']:
      self.cleaner_alert()
      self.hvac_on()

  def _get_rental_events(self, unit_code: str) -> RentalEventInfo:
    """ Calculate the checkin / active / checkout rental control events
    Rental control integration doesn't provide checkin/checkout reservation
    events, just a sequence of "relevant" events (ie, recent and future
    reservations).
    For example, if there's a checkout and checkin today then the first event
    is the checkout and the second event is the checkin. But if the checkout
    was yesterday then the first event is the checkin.
    This looks at the first and second event and determines which, if any,
    are the checkout / checkin / active events.

    Args:
        unit_code (str): Unit Code as used in the Rental Control setup

    Returns:
        RentalEventInfo: Applicable calendar events
    """
    events: list[CalendarEventInfo] = []

    today = date.today()

    # Get reservation start & end dates for first & second calendar events
    for evt_idx in (0, 1):
      evt_name = f'sensor.rental_control_{ unit_code }_event_{ evt_idx }'
      events.append({
        'name': evt_name,
        'start_date': self._get_state_datetime(evt_name, 'start').date(),
        'end_date': self._get_state_datetime(evt_name, 'end').date(),
      })

    # Checkin Today
    checkin_today: str | None = None
    if events[1]['start_date'] == today:
      checkin_today = events[1]['name']
    elif events[0]['start_date'] == today:
      checkin_today = events[0]['name']

    # Active Checkin
    checkin_active: str | None = None
    if events[1]['start_date'] <= today:
      checkin_active = events[1]['name']
    elif events[0]['start_date'] <= today:
      checkin_active = events[0]['name']

    # Checkout Today
    checkout_today: str | None = None
    if events[0]['end_date'] == today:
      checkout_today = events[0]['name']

    return {
      'checkin_today_evt': checkin_today,
      'checkin_active_evt': checkin_active,
      'checkout_today_evt': checkout_today,
    }


  def reset_checkin_time(self):
    """ Reset the checkin time input entity.
    This happens every day right after midnight; the custom checkin time won't
    persist across days.
    """
    # TODO: Only reset after checkin
    ent_key_checkin_time = (f'input_datetime.str_{ self.unit['code'] }_'
                            'checkin_time')
    db_key = 'last_checkin_reset'

    # By comparing dates then this will execute right after midnight
    if not self._db_is_today(db_key):
      self.log('Resetting checkin time to %s', self.default_checkin_time)
      self.call_service(
          'input_datetime/set_datetime',
          service_data={'time': self.default_checkin_time},
          target={'entity_id': ent_key_checkin_time} )
      self._db_set_today(db_key)


  def cleaner_alert(self):
    """Check if cleaner has arrived on time; alert if not.
    """
    ####### Check that the cleaners have started
    db_key = 'last_cleaner_check'

    if (datetime.now().time() > self.cleaner_check_time
        and not self._db_is_today(db_key)):

      unlock_times = self._get_last_unlocks()
      if not unlock_times['cleaner_unlock'] or not unlock_times['guest_unlock']:
        # This is unlikely, and probably means something is wrong
        # Log an error, but we'll automatically try again soon
        self.error('Could not determine last unlock times: %s', unlock_times)

      assert unlock_times['cleaner_unlock'] and unlock_times['guest_unlock']

      if unlock_times['guest_unlock'] > unlock_times['cleaner_unlock']:
        # Guest unlocked the door more recently than the cleaning fairies
        self.log('ALERT - Checked for recent cleaning: %s', unlock_times)

        # TODO: Make configurable
        # send alerts
        # Send page email
        self.call_service(
          'notify/mail_page_amzn',
          service_data={'target': 'jrshann@amazon.com',
                        'title': f'[{ self.unit["name"] }] Check Cleaners',
                        'message': f'Check cleaners for { self.unit["name"] }'})

        # Send Maria SMS
        self.call_service(
          'notify/mail_page_amzn',
          service_data={'target': '14158897287@msg.fi.google.com',
                        'title': f'[{ self.unit["name"] }] Check Cleaners',
                        'message': f'Check cleaners for { self.unit["name"] }'})

      else:
        self.log('OK - Checked for recent cleaning: %s', unlock_times)

      self._db_set_today(db_key)

  def hvac_off(self):
    """Turn off the HVAC after checkout time.
    """
    ######## Turn off the AC on checkout day
    db_key = 'last_hvac_off'

    if (datetime.now().time() > self.checkout_time
        and not self._db_is_today(db_key)):
      self.log('Turning off thermostat')

      self.call_service(
          'climate/turn_off',
          target={'entity_id': self.unit['thermostat_key']})

      self._db_set_today(db_key)

  def hvac_on(self):
    """Turn on the HVAC before checkin time.
    """
    ###### Turn on the AC
    db_key = 'last_hvac_on'
    entity_key = f'input_datetime.str_{ self.unit['code'] }_checkin_time'
    checkin_time = time.fromisoformat(str(self.get_state(entity_key)))

    # TODO: Calculate the time needed to adjust the tempeature
    # TODO: Turn on heat
    # TODO: Turn on ceiling fans
    if (self._sub_time(checkin_time, datetime.now().time()) < 30
        and not self._db_is_today(db_key)):
      self.log('Turning on thermostat')
      self.call_service(
          'climate/set_temperature',
          target= {'entity_id': self.unit['thermostat_key']},
          hvac_mode='cool',
          temperature=23,
      )
      self._db_set_today(db_key)

  def _get_last_unlocks(self) -> dict[str, datetime | None]:
    """Get the most recent unlock times for cleaners and guests.

    Returns:
        dict[str, datetime | None]: Dictionary with keys 'cleaner_unlock' and
            'guest_unlock'.
    """
    cleaner_unlock = None
    guest_unlock = None

    # TODO: Make these configurable
    cleaner_re = re.compile(r'^Maria\s+Reno\s+cleaning\s+fairies',
                            re.IGNORECASE)
    guest_re = re.compile(r'^\d{2}/\d{2}', re.IGNORECASE)

    # TODO: Make the sensor entity configurable
    unlocks = self.get_history(
        f'sensor.{ self.unit["code"] }_front_door_operator',
        days=15, no_attributes='true') # type: ignore (Filed bug with AppDaemon)

    # The return will always be a list of lists, even if no events
    assert unlocks

    # Work backwards to find the most recent
    for unlock in reversed(unlocks[0]):
      if not cleaner_unlock and cleaner_re.search(unlock['state']):
        cleaner_unlock = unlock['last_changed']

      if not guest_unlock and guest_re.search(unlock['state']):
        guest_unlock = unlock['last_changed']

    return {
      'cleaner_unlock': cleaner_unlock,
      'guest_unlock': guest_unlock
    }


  def _get_state_datetime(
      self, entity_id: str, attribute: str | None = None) -> datetime:
    """Get state value from HA entity as a datetime.

    Args:
        entity_id (str): HA entity ID
        attribute (str | None, optional): HA entity attribute. Defaults to None.

    Returns:
        datetime: State value as datetime
    """
    state = self.get_state(entity_id, attribute)
    assert isinstance(state, str)
    return datetime.fromisoformat(state)


  def _sub_time(self, time1: time | datetime, time2: time) -> int:
    """Subtract time from another time.

    Args:
        time1 (time | datetime): Minuend. If datetime then only the time
            component is used.
        time2 (time): Subtrahend.

    Returns:
        int: Remainder, in minutes, rounded.
    """
    if isinstance(time1, datetime):
      time1 = time1.time()

    return int(timedelta(
      hours=time1.hour - time2.hour,
      minutes=time1.minute-time2.minute).total_seconds() / 60)


  def _db_set_today(self, key: str):
    """Shortcut to set DB record value to today (as a date).

    Args:
        key (str): DB record key
    """
    self.db[key] = date.today()


  def _db_is_today(self, key: str) -> bool:
    """Shortcut to check if DB record value is equal to today

    Args:
        key (str): DB record key

    Returns:
        bool: True if DB record is today
    """
    return self.db.get(key) == date.today()
