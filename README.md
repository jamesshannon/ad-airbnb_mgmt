This is a repository of AppDaemon apps I've made for Home Assistant.


```
airbnb_mgmt:
  module: airbnb_mgmt
  class: AirbnbManagement

  units:
    - name: Main
      code: main
      cal_code: airbnb
      thermostat_key: climate.t9_thermostat
    - name: ADU
      code: adu
      cal_code: adu_unit
      thermostat_key: climate.adu_heat_pump_heat_pump
```
