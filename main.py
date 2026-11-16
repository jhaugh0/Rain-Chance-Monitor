import ntptime
import time
import network
import machine
import os
import json
import urequests as r
from neopixel import NeoPixel

REPO = 'jhaugh0/Rain-Chance-Monitor'
CONFIG_FILE = "config.json"
VERSION_TRACKER_FILE = 'version.txt'
RUN_LOG = 'run.log'
ERROR_LOG = 'error.log'
Run_Log = ''
CLOCK = {}          # filled by generate_clock_map() when LED TYPE is 'clock'
PIN_DATA = None     # last forecast pulled from weather.gov (reused between LED steps)
LAST_REFRESH = 0    # time.time() of the last full network refresh

RTC = machine.RTC()
WLAN = network.WLAN(network.STA_IF)

BASE_CONFIG = {
    "TIME_BETWEEN_CHECKS_M" : 15,
    "NETWORK" : {
        "SSID" : "",
        "PSK" : "",
        "MAX_REQUEST_RETRIES" : 5,
        "REQUEST_RETRY_DELAY_SECONDS" : 5,
        "INTERNET_CHECK_RETRY_SECONDS" : 5
    },
    "LOCATION": {
        "LATITUDE" : "",
        "LONGITUDE" : "",
        "TIME_REGION" : "America/New_York"
    },
    "LED" : {
        "CABLE_SIDE" : "right",
        "TEMP_STRIP": True,
        "RAIN_GPIO_PIN" : 35,
        "TEMP_GPIO_PIN" : 37,
        "ON_HOUR" : 7,
        "OFF_HOUR" : 22,
        "FIRST_BAR_HOUR" : 8,
        "TOTAL_COUNT" : 15,
        "BRIGHTNESS" : 10,
        "YELLOW_THRESHOLD_START" : 30,
        "RED_THRESHOLD_START" : 50,
        "TYPE" : "bar",              # "bar" (default) or "clock"
        "CLOCK_FIRST_HOUR" : 12,     # clock only: hour shown by LED 0 of each ring
        "CLOCK_DIRECTION" : "cw"     # clock only: "cw" or "ccw", direction LED numbers increase
    }
}

OFF = (0, 0, 0)
NAMED = {
    'red':    (1, 0, 0),
    'green':  (0, 1, 0),
    'blue':   (0, 0, 1),
    'yellow': (1, 1, 0),
    'cyan':   (0, 1, 1),
    'white':  (1, 1, 1),
    'off':    OFF,
}
BLUE_RED = {
    30: (0,  0,  1),
    40: (.1, .2, .9),
    50: (.2, .3, .5),
    60: (.3, .5, .3),
    70: (.5, .3, .2),
    80: (.9, .2, .1),
    90: (1,  0,  0),
}
GREEN_RED = {
    0:   (0,  1,  0),
    10:  (.3, .8, 0),
    20:  (.4, .7, 0),
    30:  (.5, .6, 0),
    40:  (.6, .5, 0),
    50:  (.7, .4, 0),
    60:  (.8, .3, 0),
    70:  (.9, .3, 0),
    80:  (.9, .2, 0),
    90:  (.9, .1, 0),
    100: (1,  0,  0),
}


class Check_for_updates():
    def __init__(self):
        log('Initializing update checker')
        self.Main_file_url = 'https://raw.githubusercontent.com/' + REPO + '/refs/heads/main/main.py'
        self.Version_hash_url = 'https://api.github.com/repos/' + REPO + '/branches/main'
        self.Request_headers = {'user-agent':os.uname().sysname}
    def get_version_from_disk(self):
        log('  >> Getting version from disk')
        if VERSION_TRACKER_FILE in os.listdir():
            try:
                with open(VERSION_TRACKER_FILE, 'r') as f:
                    version = f.read()
                log(f'    >> {version} found on disk')
            except Exception as e:
                log(f'    >> Failed to read version file. error: {e}')
                return
        else:
            log('    >> No version file found on disk, assuming version 0')
            version = '0'
        return version
    def get_version_from_github(self):
        try:
            log('  >> Trying to get latest github repo version hash')
            response = r.get(self.Version_hash_url, headers=self.Request_headers)
            version = response.json()['commit']['sha']
            log(f'    >> {version} retrieved')
            return version
        except Exception as e:
            log(f'    >> Failed. Error: {e}')
            return ''
    def get_latest_file_version(self):
        log('  Getting latest version of main.py')
        request = r.get(self.Main_file_url, headers=self.Request_headers)
        if request.status_code == 200:
            log(f'    >> Request succeeded, new file is {len(request.content)} chars long')
            return request.content
        log('    >> Failed getting latest version of main.py')
        return ''
    def write_new_version(self, file, version):
        log('    >> Writing new version file')
        with open(VERSION_TRACKER_FILE, 'w') as f:
            f.write(version)
        log('  >> Writing new main.py')
        with open('main.py', 'w') as f:
            f.write(file)
    def main(self):
        if HOUR != CONFIG['LED']['ON_HOUR'] and HOUR != CONFIG['LED']['OFF_HOUR'] and MINUTE != 0:
            log('  >> Not the ON_HOUR or OFF_HOUR, skipping update check')
            set_LEDs(strip=NP['RAIN'], color='yellow', brightness=1)
            return
        current_version = self.get_version_from_disk()
        latest_version = self.get_version_from_github()
        if latest_version == '':
            log('  >> Failed getting newest version hash')
            return
        if current_version != latest_version:
            log(f'  >> New version found\n  Current version: {current_version}\n  New version:     {latest_version}')
            content = self.get_latest_file_version()
            if content == '':
                log('  >> Failed getting latest main.py file, aborting update')
                return
            self.write_new_version(file=content, version=latest_version)
            log('\nResetting')
            machine.reset()
        else:
            log(f'No new version found\n  Current version: {current_version}\n  Latest version:  {latest_version}')

class WeatherGOV():
    def __init__(self):
        log('Getting Weather Data from weather.gov')
        self.base = 'https://api.weather.gov'
        self.latitude = str(round(float(CONFIG['LOCATION']['LATITUDE']), 4))
        self.longitude = str(round(float(CONFIG['LOCATION']['LONGITUDE']), 4))
        self.headers = {'user-agent':'unpadded_viselike357@simplelogin.com'}
    def get_point(self):
        log('  >> Getting point data/endpoint by geo coords')
        url = self.base + '/points/' + self.latitude + ',' + self.longitude
        point = r.get(url, headers=self.headers)
        forecast_endpoint = point.json()['properties']['forecastHourly']
        return forecast_endpoint
    def get_forecast(self, endpoint):
        log('  >> Getting forecast data from endpoint')
        forecast = r.get(endpoint, headers=self.headers)
        return forecast.json()
    def filter_forecast(self, forecast):
        log('  >> Filtering forecast data')
        hours = {}
        for hourData in forecast['properties']['periods']:
            day = int(hourData['startTime'].split('T')[0].split('-')[2])
            hour = int(hourData['startTime'].split('T')[1].split(':')[0])
            if day != DAY and hour == HOUR:
                print(f'    >> Data from time {hourData['startTime']} is too far outside the usable range, stopping loop')
                break
            hours[hour] = {}
            hours[hour]['rain'] = hourData['probabilityOfPrecipitation']['value']
            hours[hour]['temp'] = hourData['temperature']
        return hours
    def main(self):
        endpoint = self.get_point()
        forecast = self.get_forecast(endpoint)
        filtered = self.filter_forecast(forecast)
        return filtered   

def delay():
    now = time.localtime()
    seconds_into_hour = (now[4] * 60) + now[5]
    if is_clock() and not is_off_hours():
        # Wake exactly when the clock hand crosses into the next outer-ring LED,
        # i.e. 3600 / LEDs_per_hour seconds apart (360s for 10 LEDs per hour).
        per_hour = CLOCK['PER_HOUR']['RAIN']
        next_led = (seconds_into_hour * per_hour // 3600) + 1
        next_boundary = -(-next_led * 3600 // per_hour)      # ceil, handles uneven divisions
        delay = max(1, next_boundary - seconds_into_hour)
    else:
        seconds_to_hour = 3600 - seconds_into_hour
        check_seconds = CONFIG['TIME_BETWEEN_CHECKS_M'] * 60
        delay = seconds_to_hour if seconds_to_hour <= check_seconds else check_seconds
    log(f'  Delaying for {delay} seconds, resume time: {time.localtime(time.time() + delay)}')
    time.sleep(delay)

def load_config():
    global CONFIG
    log('Loading configuration')
    try:
        with open(CONFIG_FILE, 'r') as f:
            log(f'  Reading configuration from {CONFIG_FILE}')
            config = json.loads(f.read())
            log(f'  >> Configuration loaded: {config}')
    except:
        log('Failed to load configuration, writing default test config')
        write_test_config()
        load_config()
    update = False
    for key, value in BASE_CONFIG.items():
        if key not in config:
            config[key] = value
            update = True
    if update:
        with open(CONFIG_FILE, 'w') as f:
            f.write(json.dumps(config))
    CONFIG = config

def dump_current_config():
    with open(CONFIG_FILE, 'w') as f:
        f.write(json.dumps(CONFIG))

def init_neopixel():
    log('Initializing NeoPixel Variables')
    global NP
    NP = {}
    rain_pin = machine.Pin(CONFIG['LED']['RAIN_GPIO_PIN'], machine.Pin.OUT)
    if is_clock():
        temp_pin = machine.Pin(CONFIG['LED']['TEMP_GPIO_PIN'], machine.Pin.OUT)
        NP['RAIN'] = NeoPixel(rain_pin, CONFIG['LED']['OUTER_RING'])
        NP['TEMP'] = NeoPixel(temp_pin, CONFIG['LED']['INNER_RING'])
    else:
        NP['RAIN'] = NeoPixel(rain_pin, CONFIG['LED']['TOTAL_COUNT'])
        if CONFIG['LED']['TEMP_STRIP']:
            temp_pin = machine.Pin(CONFIG['LED']['TEMP_GPIO_PIN'], machine.Pin.OUT)
            NP['TEMP'] = NeoPixel(temp_pin, CONFIG['LED']['TOTAL_COUNT'])

def make_network_request_with_retry(url, message):
    log(f'  Making GET request to {url}')
    retries = 0
    while retries < CONFIG['NETWORK']['MAX_REQUEST_RETRIES']:
        try:
            response = r.get(url)
            return response.json()
        except:
            log(f'  {message}, retry {retries}/{CONFIG['NETWORK']['MAX_REQUEST_RETRIES']}')
            retries = retries + 1
            log(f'  Pausing {CONFIG['NETWORK']['REQUEST_RETRY_DELAY_SECONDS']} seconds before next attempt')
            time.sleep(CONFIG['NETWORK']['REQUEST_RETRY_DELAY_SECONDS'])
        if 'response' in locals():
            log(f'  >> status code: {response.status_code}')
        if retries == CONFIG['NETWORK']['MAX_REQUEST_RETRIES']:
            return None

def get_local_timenow_time(useLEDs=True):
    log('Getting local time from time.now')
    if is_off_hours():
        useLEDs = False
    url = 'https://time.now/developer/api/timezone/'
    url = url + CONFIG['LOCATION']['TIME_REGION']
    response = make_network_request_with_retry(url, message='Failed to get time')
    if response:
        global HOUR, DAY
        log(f'  >> Local time {response['datetime']} returned')
        HOUR = int(response['datetime'].split('T')[1].split(":")[0])
        DAY = int(response['datetime'].split('T')[0].split("-")[2])
        log(f'  >> Parsed time: {HOUR}:{DAY}')
        if useLEDs:
            set_LEDs(strip=NP['TEMP'], color='yellow', brightness=1)

def _nth_sunday(year, month, n):
    wd = time.localtime(time.mktime((year, month, 1, 0, 0, 0, 0, 0)))[6]  # Mon=0
    return 1 + (6 - wd) % 7 + 7 * (n - 1)

def local_time_dst(std_offset=-5 * 3600):
    global HOUR, DAY, MINUTE, SECOND
    utc = time.time()
    y = time.localtime(utc)[0]
    start = time.mktime((y, 3, _nth_sunday(y, 3, 2), 2, 0, 0, 0, 0)) - std_offset
    end = time.mktime((y, 11, _nth_sunday(y, 11, 1), 2, 0, 0, 0, 0)) - (std_offset + 3600)
    offset = std_offset + 3600 if start <= utc < end else std_offset
    offset_time = time.localtime(utc + offset)
    DAY = offset_time[2]
    HOUR = offset_time[3]
    MINUTE = offset_time[4]
    SECOND = offset_time[5]

def manage_wifi(action='connect', useLEDs=True):
    if is_off_hours():
        useLEDs = False
    if action == 'connect':
        log('Connecting WiFi')
        log(f'  Using wifi info:\n    SSID: {CONFIG['NETWORK']['SSID']}\n    PSK:  {CONFIG['NETWORK']['PSK']}')
        if not WLAN.isconnected():
            if useLEDs:
                set_LEDs(color='off')
            WLAN.active(True)
            WLAN.config(pm=WLAN.PM_NONE)
            time.sleep(.5)
            WLAN.scan()
            WLAN.connect(CONFIG['NETWORK']['SSID'], CONFIG['NETWORK']['PSK'])
            if os.uname().sysname == 'rp2':
                log('Disabling rp2 specific WiFi power saving settings')
                WLAN.config(pm = 0xa11140)
            start_pin = 0
            while True:
                log(f'    IP: {WLAN.ifconfig()[0]}. StartPin: {start_pin}')
                if WLAN.ifconfig()[0] == '0.0.0.0':
                    if useLEDs:
                        start_pin = set_LEDs(strip=NP['RAIN'], color='cyan', startPin=start_pin, brightness=5)
                    time.sleep(1)
                else:
                    log('Connected!')
                    log(f'  >> IP: {WLAN.ifconfig()[0]}')
                    if useLEDs:
                        set_LEDs(color='white', brightness=1)
                    break
        else:
            log(f"Already connected to wifi: {str(WLAN.ifconfig())}")
            return
    elif action == 'disconnect':
        log('Disconnecting WiFi')
        if WLAN.active():
            WLAN.disconnect()
            log('Disabling WiFi')
            WLAN.active(False)
        else:
            log('WiFi is already disabled')

def validate_internet_connection(tries_before_reconnect=10, max_tries=20, useLEDs=True):
    log('Validating public internet connection')
    retries = 0
    if is_off_hours():
        useLEDs = False
    while True:
        try:
            response = r.get('https://ip.me')
            if response.status_code == 200:
                log(f'  Internet appears to be connected, Public IP: {response.text.strip()}')
                if useLEDs:
                    set_LEDs(strip=NP['TEMP'], color='blue', brightness=1)
                return True
        except Exception as e:
            if retries == max_tries:
                log(f'  Internet connection not functional yet. Hit max retry count of {max_tries}')
                log('\n\nResetting completely.')
                machine.reset()
            if retries % tries_before_reconnect == 0 and retries != 0:
                log(f'  Internet connection not functional yet. Retry #{retries}/{max_tries}. Reconnecting wifi to troubleshoot')
                manage_wifi('disconnect')
                log('Delaying 20 seconds')
                time.sleep(20)
                manage_wifi('connect')
            else:
                log(f'  Internet connection not functional yet. Retry #{retries}/{max_tries}. Trying again in {CONFIG['NETWORK']['INTERNET_CHECK_RETRY_SECONDS']} seconds')
                log(f'Error: {e}')
            time.sleep(CONFIG['NETWORK']['INTERNET_CHECK_RETRY_SECONDS'])
            retries = retries + 1

def update_RTC(useLEDs=True):
    log('Updating RTC time')
    log("  Local time before synchronization：%s" %str(time.localtime()))
    if is_off_hours():
        useLEDs = False
    retries = 0
    while retries < CONFIG['NETWORK']['MAX_REQUEST_RETRIES']:
        try:
            ntptime.settime()
            log("  Local time after synchronization：%s" %str(time.localtime()))
            if useLEDs:
                set_LEDs(strip=NP['RAIN'], color='blue', brightness=1)
            return
        except:
            log(f'  Failed to get NTP time, retry {retries+1}/{CONFIG['NETWORK']['MAX_REQUEST_RETRIES']}')
            retries = retries + 1
            log(f'  Pausing {CONFIG['NETWORK']['REQUEST_RETRY_DELAY_SECONDS']} seconds before next attempt')
        if retries == CONFIG['NETWORK']['MAX_REQUEST_RETRIES']:
            return None

def get_color(color, brightness, blueRed=False, greenRed=False):
    level = round(255 * brightness * .01)
    if blueRed:
        table, key = BLUE_RED, min(max(round(color / 10) * 10, 30), 90)
    elif greenRed:
        table, key = GREEN_RED, round(color / 10) * 10
    else:
        table, key = NAMED, color
    return tuple(round(level * f) for f in table.get(key, OFF))

def set_pixel(strip, i, rgb):
    if strip[i] == rgb:
        return False
    strip[i] = rgb
    return True

def fill_strips(strips, rgb):
    for s in strips:
        if all(s[i] == rgb for i in range(len(s))):
            continue
        s.fill(rgb)
        s.write()

def set_LEDs(strip=None, pinMap=None, color='', brightness=50, RGBValue=(0, 0, 0), startPin=None, blueRed=False, greenRed=False):
    brightness = min(brightness, 100)
    def rgb_for(value, level=brightness):
        return get_color(value, level, blueRed, greenRed)
    if RGBValue != (0, 0, 0):
        fill_strips(NP.values(), RGBValue)
    elif pinMap:
        changed = False
        if is_clock():
            key = 'RAIN' if strip is NP['RAIN'] else 'TEMP'
            changed = set_clock_pixels(strip, key, pinMap, brightness, blueRed, greenRed)
        else:
            for hour, value in pinMap.items():
                if hour == HOUR:
                    level = 100
                elif hour < HOUR:
                    level = round(brightness / 4)
                else:
                    level = brightness
                changed |= set_pixel(strip, HOURS_MAP.index(hour), rgb_for(value, level))
        if changed:
            strip.write()          # one write per pass, and only if something changed
    elif startPin is not None:
        if startPin >= CONFIG['LED']['TOTAL_COUNT']:
            set_LEDs(color='off')
            return 0
        rgb = rgb_for(color)       # computed once instead of three times
        for s in NP.values():
            if set_pixel(s, startPin, rgb):
                s.write()
        return startPin + 1
    elif strip is not None and color != '':
        fill_strips([strip], rgb_for(color))
    else:
        fill_strips(NP.values(), rgb_for(color))

def create_pin_dict(clock):
    log('Initializing pin dictionary')
    pin_data = {}
    if clock:
        return pin_data     # clock mode just uses whatever hours the forecast returns
    for hour in range(CONFIG['LED']['FIRST_BAR_HOUR'], CONFIG['LED']['FIRST_BAR_HOUR']+CONFIG['LED']['TOTAL_COUNT']):
        pin_data[hour] = None 
    return pin_data

def is_clock():
    return CONFIG['LED'].get('TYPE') == 'clock'

def generate_clock_map():
    #Work out how the two rings are split into per-hour groups of LEDs.
    global CLOCK
    led = CONFIG['LED']
    outer_per, inner_per = [int(x) for x in led.get('CLOCK_RATIO', '10:4').split(':')]
    hours = led['OUTER_RING'] // outer_per
    if hours * outer_per != led['OUTER_RING'] or hours * inner_per != led['INNER_RING']:
        raise ValueError('OUTER_RING / INNER_RING do not divide evenly by CLOCK_RATIO')
    CLOCK = {
        'HOURS': hours,                                    # hour groups around each ring (12)
        'PER_HOUR': {'RAIN': outer_per, 'TEMP': inner_per},  # LEDs per hour group, per ring
        'FIRST': led.get('CLOCK_FIRST_HOUR', 12) % hours,
        'CW': led.get('CLOCK_DIRECTION', 'cw') == 'cw',
    }
    log(f'Clock map: {hours} hour groups, LEDs per hour {CLOCK["PER_HOUR"]}')

def clock_group(hour):
    #Position (0..HOURS-1) of an hour's LED group around the ring.
    offset = (hour - CLOCK['FIRST']) % CLOCK['HOURS']
    return offset if CLOCK['CW'] else (-offset) % CLOCK['HOURS']

def clock_marker(per_hour):
    #Index (within the current hour's group) of the LED nearest the current time.
    seconds = (MINUTE * 60) + SECOND
    return min(seconds * per_hour // 3600, per_hour - 1)

def set_clock_pixels(strip, key, pinMap, brightness, blueRed=False, greenRed=False):
    #Paint a ring: the next HOURS hours, one group of LEDs per hour.
    #In the current hour's group the LED at the current time is 100%, LEDs already passed are dimmed.
    per_hour = CLOCK['PER_HOUR'][key]
    mark = clock_marker(per_hour)
    changed = False
    for ahead in range(CLOCK['HOURS']):
        hour = (HOUR + ahead) % 24
        value = pinMap.get(hour)
        start = clock_group(hour) * per_hour
        for i in range(per_hour):
            if ahead == 0 and i == mark:
                level = 100
            elif ahead == 0 and i < mark:
                level = round(brightness / 4)
            else:
                level = brightness
            rgb = get_color(value, level, blueRed, greenRed) if isinstance(value, (int, float)) else OFF
            changed |= set_pixel(strip, start + i, rgb)
    return changed

def generate_bar_hours_map():
    global HOURS_MAP
    hoursRange = range(CONFIG['LED']['FIRST_BAR_HOUR'], CONFIG['LED']['FIRST_BAR_HOUR']+CONFIG['LED']['TOTAL_COUNT'])
    if CONFIG['LED']['CABLE_SIDE'] == 'right':
        HOURS_MAP = list(reversed(hoursRange))
    else:
        HOURS_MAP = list(hoursRange)

def map_hours_to_pins(clock):
    log('Mapping hours to pins')
    pinData = create_pin_dict(clock)
    hoursMap = WeatherGOV().main()
    log('  Mapping hour data to pin data')
    
    if clock:
        for hour in hoursMap.keys():
            log(f'    >> Mapping hour {hour} to {hoursMap[hour]}')
            pinData[hour] = hoursMap[hour]
    else:
        for hour in pinData:
            if hour in hoursMap.keys():
                log(f'    >> Mapping hour {hour} to {hoursMap[hour]}')
                pinData[hour] = hoursMap[hour]
            else:
                log(f'    >> Mapping hour {hour} to off')
                pinData[hour] = {'rain': 'off', 'temp': 'off'}
    set_LEDs(strip=NP['TEMP'], color='cyan', brightness=1)
    return pinData

def is_off_hours():
    if 'HOUR' not in globals():
        return False
    return HOUR >= CONFIG['LED']['OFF_HOUR'] or HOUR < CONFIG['LED']['ON_HOUR']

def send_map_to_leds(pinData):
    if is_clock():
        local_time_dst()    # network calls take a while, re-read the time right before drawing
    rainData = {}
    for hour in pinData.keys():
        value = pinData[hour]
        rainData[hour] = value['rain']
    set_LEDs(strip=NP['RAIN'], pinMap=rainData, brightness=CONFIG['LED']['BRIGHTNESS'], greenRed=True)
    if CONFIG['LED']['TEMP_STRIP']:
        tempData = {}
        for hour in pinData.keys():
            value = pinData[hour]
            tempData[hour] = value['temp']
        set_LEDs(strip=NP['TEMP'], pinMap=tempData, brightness=CONFIG['LED']['BRIGHTNESS'], blueRed=True)

def write_error_log(message):
    log('Writing error log')
    with open(ERROR_LOG, 'w') as f:
        f.write(message)

def print_error_log():
    if ERROR_LOG in os.listdir():
        log('Printing error log')
        with open(ERROR_LOG, 'r') as f:
            print(f.read())

def print_run_log():
    if RUN_LOG in os.listdir():
        log('Printing error log')
        with open(RUN_LOG, 'r') as f:
            print(f.read())

def write_test_config():
    with open(CONFIG_FILE, 'w') as f:
        f.write(json.dumps(BASE_CONFIG))

def log(message, initialize=False, write_to_file=False):
    global Run_Log
    print(message)
    if initialize:
        Run_Log = message + '\n'
        return
    Run_Log = Run_Log + message + '\n'
    if write_to_file:
        with open(RUN_LOG, 'w') as f:
            f.write(Run_Log)
        return

def refresh_due():
    return PIN_DATA is None or time.time() - LAST_REFRESH >= CONFIG['TIME_BETWEEN_CHECKS_M'] * 60

def main_loop(initialized, clock):
    global PIN_DATA, LAST_REFRESH
    log('Starting Main Loop', initialize=True)
    useLEDs = False if initialized else True
    try:
        if clock and initialized and not refresh_due():
            # Clock mode wakes every LED step: no network, just move the marker using the cached forecast
            log('Clock step: advancing marker from cached forecast')
            local_time_dst()
            if is_off_hours():
                set_LEDs(color='off')
                return
            send_map_to_leds(PIN_DATA)
            return
        manage_wifi('connect')
        validate_internet_connection(useLEDs=useLEDs)
        update_RTC(useLEDs)
        local_time_dst()
        if is_off_hours():
            log('It is currently off hours, skipping LED update.')
            set_LEDs(color='off')
            return
        Check_for_updates().main()
        hourMap = map_hours_to_pins(clock)
        PIN_DATA, LAST_REFRESH = hourMap, time.time()
        send_map_to_leds(hourMap)
        return
    except Exception as e:
        write_error_log(str(e))
        log(f'Error occurred: {e}', write_to_file=True)

def main():
    print('Starting up....')
    intialized = False
    load_config()
    clock = is_clock()
    init_neopixel()
    set_LEDs(color='cyan', brightness=10)
    if clock:
        generate_clock_map()
    else:
        generate_bar_hours_map()
    while True:
        main_loop(intialized, clock)
        delay()
        intialized = True

main()