from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
import os
import shelve
import typing as t

from appdaemon.plugins.hass import Hass # pyright: ignore

# pyright: reportUnknownMemberType=false

MY_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(MY_DIR, 'state')

class Unit(t.TypedDict):
  name: str
  code: str
  cal_code: str
  thermostat_key: str

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
    self.db = shelve.open(DB_PATH)
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
    self.units = t.cast(list[Unit], self.args.get('units'))

    # Execute the checks immediately, and every x minutes
    self.run_in(self.check_mgmt, delay=2)
    self.run_every(self.check_mgmt, interval=(self.check_interval_mins * 60))


  def terminate(self):
    """ AppDaemon app cleanup

    Close the database
    """
    self.db.close()

  def check_mgmt(self, **kwargs):
    self.log('Executing STR Management activities')

    for unit in self.units:
      rental_events = self._get_rental_events(unit['cal_code'])

      self.reset_checkin_time(unit)

      if rental_events['checkout_today_evt']:
        self.hvac_off(unit)

      if rental_events['checkin_today_evt']:
        self.cleaner_alert(unit)
        self.hvac_on(unit)

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


  def reset_checkin_time(self, unit: Unit):
    """ Reset the checkin time input entity.
    This happens every day right after midnight; the custom checkin time won't
    persist across days.

    Args:
        unit (Unit): The unit
    """
    # TODO: Only reset after checkin
    ent_key_checkin_time = f'input_datetime.str_{ unit['code'] }_checkin_time'
    db_key = f'{ unit['code'] }_last_checkin_reset'

    # By comparing dates then this will execute right after midnight
    if not self._db_is_today(db_key):
      self.log('[%s] Resetting checkin time to %s',
          unit['name'], self.default_checkin_time)
      self.call_service(
          'input_datetime/set_datetime',
          service_data={'time': self.default_checkin_time},
          target={'entity_id': ent_key_checkin_time} )
      self._db_set_today(db_key)


  def cleaner_alert(self, unit: Unit):
    """Check if cleaner has arrived on time; alert if not.

    Args:
        unit (Unit): The unit
    """
    ####### Check that the cleaners have started
    db_key = f'{ unit['code'] }_last_cleaner_check'

    if (datetime.now().time() >self.cleaner_check_time
        and not self._db_is_today(db_key)):

      guest_sensor = f'sensor.{ unit['code'] }_front_door_last_seen_guest'
      fairies_sensor = (f'sensor.{ unit['code'] }_front_door_last_seen_'
                         'cleaner_cleaning_fairies')
      guest_unlock = self._get_state_datetime(guest_sensor)
      fairies_unlock = self._get_state_datetime(fairies_sensor)
      log_obj = {'guest_unlock': guest_unlock, 'fairies_unlock': fairies_unlock}

      if guest_unlock > fairies_unlock:
        # Guest unlocked the door more recently than the cleaning fairies
        self.log('[%s] - ALERT - Checked for recent cleaning: %s',
            unit['name'], log_obj)


        # 2025-09-14 22:49:12.914903 WARNING HASS: Error with websocket result: invalid_format: required key not provided @ data['message']
        # send alerts
        # Send page email
        self.call_service(
            'notify/mail_page_amzn',
            service_data={'target': 'jrshann@amazon.com',
                          'title': 'Check Cleaners'})

        # Send Maria SMS
        self.call_service(
            'notify/mail_page_amzn',
            service_data={'target': '14158897287@msg.fi.google.com',
                          'title': 'Check Cleaners'})

      else:
        self.log('[%s] - OK - Checked for recent cleaning: %s',
            unit['name'], log_obj)


      self._db_set_today(db_key)

  def hvac_off(self, unit: Unit):
    """Turn off the HVAC after checkout time.

    Args:
        unit (Unit): The unit
    """
    ######## Turn off the AC on checkout day
    db_key = f'{ unit['code'] }_last_hvac_off'
    if (datetime.now().time() > self.checkout_time
        and not self._db_is_today(db_key)):
      self.log('[%s] Turning off thermostat', unit['name'])
      self.call_service(
          'climate/turn_off',
          target={'entity_id': unit['thermostat_key']})
      self._db_set_today(db_key)

  def hvac_on(self, unit: Unit):
    """Turn on the HVAC before checkin time.

    Args:
        unit (Unit): The unit
    """
    ###### Turn on the AC
    db_key = f'{ unit['code'] }_last_hvac_on'
    entity_key = f'input_datetime.str_{ unit['code'] }_checkin_time'

    checkin_time = time.fromisoformat(str(self.get_state(entity_key)))

    # TODO: Calculate the time needed to adjust the tempeature
    # TODO: Turn on heat
    # TODO: Turn on ceiling fans
    if (self._sub_time(checkin_time, datetime.now().time()) < 30
        and not self._db_is_today(db_key)):

      self.log('[%s] Turning on thermostat', unit['name'])
      self.call_service(
          'climate/set_temperature',
          target= {'entity_id': unit['thermostat_key']},
          hvac_mode='cool',
          temperature=23,
      )
      self._db_set_today(db_key)

  def _get_state_time(
      self, entity_id: str, attribute: str | None = None) -> time:
    """Get state value from HA entity as a time.

    Args:
        entity_id (str): HA entity ID
        attribute (str | None, optional): HA entity attribute. Defaults to None.

    Returns:
        datetime: State value as time
    """
    return self._get_state_datetime(entity_id, attribute).time()

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
