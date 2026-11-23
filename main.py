import ntptime
import time
import network
import machine
import os
import gc
import json
import socket
import select
import binascii
import urequests as r
from neopixel import NeoPixel

REPO = 'jhaugh0/Rain-Chance-Monitor'
CONFIG_FILE = "config.json"
VERSION_TRACKER_FILE = 'version.txt'
RUN_LOG = 'run.log'
ERROR_LOG = 'error.log'
FORCE_SETUP_FILE = 'force_setup'   # written by the web page to boot straight into the setup hotspot
Run_Log = ''
CLOCK = {}          # filled by generate_clock_map() when LED TYPE is 'clock'
PIN_DATA = None     # last forecast pulled from weather.gov (reused between LED steps)
LAST_REFRESH = 0    # time.time() of the last full network refresh
FORCE_REFRESH = False   # set by the web page to pull a new forecast immediately

RTC = machine.RTC()
WLAN = network.WLAN(network.STA_IF)
AP = network.WLAN(network.AP_IF)
AP_MODE = False         # True while broadcasting the setup hotspot
CONFIG_ERROR = ''       # set if the LEDs could not be initialised from the config
WEB = None              # listening HTTP socket
DNS = None              # captive-portal DNS socket (hotspot mode only)
POLLER = None
RESTART_PENDING = False
UPTIME_MS = 0
_LAST_TICKS = time.ticks_ms()

BASE_CONFIG = {
    "TIME_BETWEEN_CHECKS_M" : 15,
    "AUTO_UPDATE" : True,                  # pull main.py from GitHub at ON_HOUR / OFF_HOUR
    "NETWORK" : {
        "SSID" : "",
        "PSK" : "",
        "MAX_REQUEST_RETRIES" : 5,
        "REQUEST_RETRY_DELAY_SECONDS" : 5,
        "INTERNET_CHECK_RETRY_SECONDS" : 5,
        "CONNECT_TIMEOUT_SECONDS" : 30,    # give up on SSID after this long and start the hotspot
        "AP_PSK" : "",                     # hotspot password, 8+ chars, blank = open hotspot
        "AP_RETRY_MINUTES" : 10            # while in hotspot mode, retry SSID this often (0 = never)
    },
    "HARDWARE" : {
        "RAIN_GPIO_PIN" : 35,
        "TEMP_GPIO_PIN" : 37,
        "TYPE" : "clock",
        "CLOCK_CONFIG" : {
            "DIRECTION" : "cw",
            "OUTER_RING" : 120,
            "INNER_RING" : 48,
            "FIRST_HOUR" : 12
        },
        "BAR_CONFIG" : {
            "CABLE_SIDE" : "right",
            "TEMPERATURE_STRIP": True,
            "FIRST_HOUR" : 8,
            "TOTAL_COUNT" : 15
        }
    },
    "TIME" : {
        "TIMEZONE" : "America/New_York",
        "DST" : True                 # follow US daylight saving (ignored for zones that don't observe it)
    },
    "LOCATION": {
        "LATITUDE" : "",
        "LONGITUDE" : ""
    },
    "LED" : {                        # applies to both bar and clock displays
        "ON_HOUR" : 7,
        "OFF_HOUR" : 22,
        "BRIGHTNESS" : 10
    },
    "WEB" : {
        "PORT" : 80,
        "PASSWORD" : ""              # blank = no login; otherwise user "admin" + this password
    }
}

# Settings that only take effect after a restart. A section path (e.g. 'HARDWARE')
# covers everything inside it, including settings added later from the web page.
RESTART_KEYS = ('NETWORK.SSID', 'NETWORK.PSK', 'NETWORK.AP_PSK', 'WEB.PORT', 'HARDWARE')

# (name, standard UTC offset in hours, observes DST). weather.gov is US-only, so US zones.
TIMEZONE_LIST = (
    ('America/New_York', -5, True),
    ('America/Chicago', -6, True),
    ('America/Denver', -7, True),
    ('America/Phoenix', -7, False),
    ('America/Los_Angeles', -8, True),
    ('America/Anchorage', -9, True),
    ('Pacific/Honolulu', -10, False),
    ('America/Puerto_Rico', -4, False),
)
TIMEZONES = {name: (hours, dst) for name, hours, dst in TIMEZONE_LIST}

# Settings limited to a fixed set of values (shown as drop-downs on the web page).
CHOICES = {
    'HARDWARE.TYPE': ['bar', 'clock'],
    'HARDWARE.CLOCK_CONFIG.DIRECTION': ['cw', 'ccw'],
    'HARDWARE.BAR_CONFIG.CABLE_SIDE': ['left', 'right'],
    'TIME.TIMEZONE': [z[0] for z in TIMEZONE_LIST],
}

MISSING = object()

OFF = (0, 0, 0)
NAMED = {
    'red':    (1, 0, 0),
    'green':  (0, 1, 0),
    'blue':   (0, 0, 1),
    'yellow': (1, 1, 0),
    'cyan':   (0, 1, 1),
    'magenta':(1, 0, 1),
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
        if not CONFIG.get('AUTO_UPDATE', True):
            log('  >> AUTO_UPDATE is disabled, skipping update check')
            return
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

def wait(seconds, interruptible=False):
    # Drop-in for time.sleep() that keeps answering web requests while it waits.
    # interruptible=True returns early when the web page asks for a refresh.
    end = time.ticks_add(time.ticks_ms(), int(seconds * 1000))
    while True:
        remaining = time.ticks_diff(end, time.ticks_ms())
        if remaining <= 0 or (interruptible and FORCE_REFRESH):
            return
        step = min(remaining, 250)
        if not service_web(step):
            time.sleep_ms(step)

def delay():
    now = time.localtime()
    seconds_into_hour = (now[4] * 60) + now[5]
    if is_clock() and not is_off_hours():
        # Wake exactly when the clock hand crosses into the next outer-ring LED,
        per_hour = CLOCK['PER_HOUR']['RAIN']
        next_led = (seconds_into_hour * per_hour // 3600) + 1
        next_boundary = -(-next_led * 3600 // per_hour)      # ceil, handles uneven divisions
        delay = max(1, next_boundary - seconds_into_hour)
    else:
        seconds_to_hour = 3600 - seconds_into_hour
        check_seconds = CONFIG['TIME_BETWEEN_CHECKS_M'] * 60
        delay = seconds_to_hour if seconds_to_hour <= check_seconds else check_seconds
    log(f'  Delaying for {delay} seconds, resume time: {time.localtime(time.time() + delay)}')
    wait(delay, interruptible=True)

def copy_value(value):
    return json.loads(json.dumps(value))

def get_path(config, path, default=None):
    # get_path(CONFIG, 'HARDWARE.CLOCK_CONFIG.OUTER_RING') -> value, or default if any level is missing
    node = config
    for part in path.split('.'):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node

def set_path(config, path, value):
    parts = path.split('.')
    node = config
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value

def del_path(config, path):
    parts = path.split('.')
    parent = get_path(config, '.'.join(parts[:-1])) if len(parts) > 1 else config
    if isinstance(parent, dict) and parts[-1] in parent:
        del parent[parts[-1]]
        return True
    return False

def merge_defaults(config, base):
    # Add any missing keys (at every level) from base into config. Returns True if anything was added.
    changed = False
    for key, value in base.items():
        if key not in config:
            config[key] = copy_value(value)
            changed = True
        elif isinstance(value, dict):
            if isinstance(config[key], dict):
                changed = merge_defaults(config[key], value) or changed
            else:
                config[key] = copy_value(value)
                changed = True
    return changed

def read_config_file():
    with open(CONFIG_FILE, 'r') as f:
        config = json.loads(f.read())
    if not isinstance(config, dict):
        raise ValueError('config is not a JSON object')
    return config

def write_config(config):
    with open(CONFIG_FILE, 'w') as f:
        f.write(json.dumps(config))

def load_config():
    global CONFIG
    log('Loading configuration')
    try:
        log(f'  Reading configuration from {CONFIG_FILE}')
        config = read_config_file()
        log(f'  >> Configuration loaded: {config}')
    except Exception as e:
        log(f'Failed to load configuration ({e}), writing default config')
        config = {}
    if merge_defaults(config, BASE_CONFIG):
        write_config(config)
    CONFIG = config

def dump_current_config():
    with open(CONFIG_FILE, 'w') as f:
        f.write(json.dumps(CONFIG))

def init_neopixel():
    log('Initializing NeoPixel Variables')
    global NP
    NP = {}
    hw = CONFIG['HARDWARE']
    rain_pin = machine.Pin(hw['RAIN_GPIO_PIN'], machine.Pin.OUT)
    if is_clock():
        ring = clock_cfg()
        temp_pin = machine.Pin(hw['TEMP_GPIO_PIN'], machine.Pin.OUT)
        NP['RAIN'] = NeoPixel(rain_pin, ring['OUTER_RING'])
        NP['TEMP'] = NeoPixel(temp_pin, ring['INNER_RING'])
    else:
        bar = bar_cfg()
        NP['RAIN'] = NeoPixel(rain_pin, bar['TOTAL_COUNT'])
        if bar['TEMPERATURE_STRIP']:
            temp_pin = machine.Pin(hw['TEMP_GPIO_PIN'], machine.Pin.OUT)
            NP['TEMP'] = NeoPixel(temp_pin, bar['TOTAL_COUNT'])

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
            wait(CONFIG['NETWORK']['REQUEST_RETRY_DELAY_SECONDS'])
        if 'response' in locals():
            log(f'  >> status code: {response.status_code}')
        if retries == CONFIG['NETWORK']['MAX_REQUEST_RETRIES']:
            return None

def _nth_sunday(year, month, n):
    wd = time.localtime(time.mktime((year, month, 1, 0, 0, 0, 0, 0)))[6]  # Mon=0
    return 1 + (6 - wd) % 7 + 7 * (n - 1)

def local_now():
    # Local time from TIME.TIMEZONE / TIME.DST (US daylight saving rules).
    settings = CONFIG.get('TIME', {})
    hours, observes_dst = TIMEZONES.get(settings.get('TIMEZONE'), (-5, True))
    std_offset = hours * 3600
    utc = time.time()
    offset = std_offset
    if observes_dst and settings.get('DST', True):
        y = time.localtime(utc)[0]
        start = time.mktime((y, 3, _nth_sunday(y, 3, 2), 2, 0, 0, 0, 0)) - std_offset
        end = time.mktime((y, 11, _nth_sunday(y, 11, 1), 2, 0, 0, 0, 0)) - (std_offset + 3600)
        if start <= utc < end:
            offset += 3600
    return time.localtime(utc + offset)

def local_time_dst():
    global HOUR, DAY, MINUTE, SECOND
    offset_time = local_now()
    DAY = offset_time[2]
    HOUR = offset_time[3]
    MINUTE = offset_time[4]
    SECOND = offset_time[5]

def hostname():
    return 'LEDWeatherMonitor' + str(CONFIG['HARDWARE'].get('TYPE', 'bar'))

def set_hostname():
    # Must run before the interface connects so DHCP (and mDNS <name>.local) use it.
    name = hostname()
    try:
        network.hostname(name)                  # MicroPython 1.20+
    except Exception:
        try:
            WLAN.config(dhcp_hostname=name)     # older firmware
        except Exception as e:
            log(f'  Could not set hostname: {e}')
            return
    log(f'Hostname set to {name}')

def manage_wifi(action='connect', useLEDs=True):
    # 'connect' returns True once connected, False if it gave up after CONNECT_TIMEOUT_SECONDS.
    if is_off_hours():
        useLEDs = False
    if action == 'connect':
        log('Connecting WiFi')
        log(f'  Using wifi info:\n    SSID: {CONFIG['NETWORK']['SSID']}\n    PSK:  {CONFIG['NETWORK']['PSK']}')
        if not WLAN.isconnected():
            if not CONFIG['NETWORK']['SSID']:
                log('  No SSID configured')
                return False
            if useLEDs:
                set_LEDs(color='off')
            set_hostname()
            WLAN.active(True)
            WLAN.config(pm=WLAN.PM_NONE)
            time.sleep(.5)
            WLAN.scan()
            WLAN.connect(CONFIG['NETWORK']['SSID'], CONFIG['NETWORK']['PSK'])
            if os.uname().sysname == 'rp2':
                log('Disabling rp2 specific WiFi power saving settings')
                WLAN.config(pm = 0xa11140)
            timeout_ms = CONFIG['NETWORK'].get('CONNECT_TIMEOUT_SECONDS', 30) * 1000
            started = time.ticks_ms()
            start_pin = 0
            while True:
                log(f'    IP: {WLAN.ifconfig()[0]}. StartPin: {start_pin}')
                if WLAN.isconnected() and WLAN.ifconfig()[0] != '0.0.0.0':
                    log('Connected!')
                    log(f'  >> IP: {WLAN.ifconfig()[0]}')
                    if useLEDs:
                        set_LEDs(color='white', brightness=1)
                    return True
                if time.ticks_diff(time.ticks_ms(), started) > timeout_ms:
                    log(f'  Could not connect to {CONFIG['NETWORK']['SSID']} within {timeout_ms // 1000} seconds')
                    try:
                        WLAN.disconnect()       # stop the driver retrying in the background
                    except Exception:
                        pass
                    return False
                if useLEDs:
                    start_pin = set_LEDs(strip=NP['RAIN'], color='cyan', startPin=start_pin, brightness=5)
                wait(1)
        else:
            log(f"Already connected to wifi: {str(WLAN.ifconfig())}")
            return True
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
                    set_LEDs(strip=temp_strip(), color='blue', brightness=1)
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
                wait(20)
                manage_wifi('connect')
            else:
                log(f'  Internet connection not functional yet. Retry #{retries}/{max_tries}. Trying again in {CONFIG['NETWORK']['INTERNET_CHECK_RETRY_SECONDS']} seconds')
                log(f'Error: {e}')
            wait(CONFIG['NETWORK']['INTERNET_CHECK_RETRY_SECONDS'])
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
            wait(CONFIG['NETWORK']['REQUEST_RETRY_DELAY_SECONDS'])
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
    if 'NP' not in globals():
        return          # LEDs failed to initialise (bad config), nothing to draw on
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
        if startPin >= min(len(s) for s in NP.values()):
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
    bar = bar_cfg()
    for hour in range(bar['FIRST_HOUR'], bar['FIRST_HOUR'] + bar['TOTAL_COUNT']):
        pin_data[hour] = None
    return pin_data

def is_clock():
    return CONFIG['HARDWARE'].get('TYPE') == 'clock'

def clock_cfg():
    return CONFIG['HARDWARE']['CLOCK_CONFIG']

def bar_cfg():
    return CONFIG['HARDWARE']['BAR_CONFIG']

def temp_strip():
    # The temperature strip/ring, or the rain strip when a bar display has no temperature strip.
    return NP['TEMP'] if 'TEMP' in NP else NP['RAIN']

def generate_clock_map():
    #Work out how the two rings are split into per-hour groups of LEDs.
    global CLOCK
    ring = clock_cfg()
    outer_per, inner_per = [int(x) for x in ring.get('RATIO', '10:4').split(':')]
    hours = ring['OUTER_RING'] // outer_per
    if hours * outer_per != ring['OUTER_RING'] or hours * inner_per != ring['INNER_RING']:
        raise ValueError('OUTER_RING / INNER_RING do not divide evenly by RATIO')
    CLOCK = {
        'HOURS': hours,                                    # hour groups around each ring (12)
        'PER_HOUR': {'RAIN': outer_per, 'TEMP': inner_per},  # LEDs per hour group, per ring
        'FIRST': ring.get('FIRST_HOUR', 12) % hours,
        'CW': ring.get('DIRECTION', 'cw') == 'cw',
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
    bar = bar_cfg()
    hoursRange = range(bar['FIRST_HOUR'], bar['FIRST_HOUR'] + bar['TOTAL_COUNT'])
    if bar['CABLE_SIDE'] == 'right':
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
    set_LEDs(strip=temp_strip(), color='cyan', brightness=1)
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
    if 'TEMP' in NP:            # clock inner ring, or bar TEMPERATURE_STRIP
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
    if len(Run_Log) > 6000:
        Run_Log = Run_Log[-4000:]       # keep RAM use bounded (the web page can show this)
    if write_to_file:
        with open(RUN_LOG, 'w') as f:
            f.write(Run_Log)
        return


# ---------------------------------------------------------------------------
# Setup hotspot + web configuration page
# ---------------------------------------------------------------------------

def start_access_point():
    global AP_MODE
    log('Starting setup hotspot')
    try:
        WLAN.disconnect()
    except Exception:
        pass
    WLAN.active(False)
    set_hostname()
    ssid = hostname()
    psk = CONFIG['NETWORK'].get('AP_PSK', '')
    AP.active(True)
    if len(psk) >= 8:
        AP.config(essid=ssid, password=psk, authmode=getattr(network, 'AUTH_WPA2_PSK', 3))
    else:
        if psk:
            log('  AP_PSK is shorter than 8 characters, hotspot will be open')
        AP.config(essid=ssid, authmode=getattr(network, 'AUTH_OPEN', 0))
    AP_MODE = True
    log(f'  Hotspot "{ssid}" is up, browse to http://{AP.ifconfig()[0]}/')

def start_web_server():
    global WEB, POLLER
    if WEB is not None:
        return
    port = int(CONFIG['WEB'].get('PORT', 80))
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(socket.getaddrinfo('0.0.0.0', port)[0][-1])
        s.listen(3)
        s.setblocking(False)
    except Exception as e:
        log(f'Failed to start web server on port {port}: {e}')
        return
    WEB = s
    POLLER = select.poll()
    POLLER.register(s, select.POLLIN)
    log(f'Web server listening on http://{current_ip()}:{port}/ (http://{hostname()}.local/)')

def start_dns():
    # Answers every DNS lookup with the hotspot's IP so phones pop up the setup page.
    global DNS
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(socket.getaddrinfo('0.0.0.0', 53)[0][-1])
        s.setblocking(False)
    except Exception as e:
        log(f'Captive portal DNS not started: {e}')
        return
    DNS = s
    if POLLER is not None:
        POLLER.register(s, select.POLLIN)

def dns_reply():
    try:
        data, addr = DNS.recvfrom(512)
    except OSError:
        return
    if len(data) < 17:
        return
    i = 12
    while i < len(data) and data[i] != 0:
        i += data[i] + 1
    question = data[12:i + 5]
    ip = bytes(int(x) for x in AP.ifconfig()[0].split('.'))
    reply = (data[:2] + b'\x81\x80' + data[4:6] + data[4:6] + b'\x00\x00\x00\x00' + question +
             b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04' + ip)
    try:
        DNS.sendto(reply, addr)
    except OSError:
        pass

def service_web(timeout_ms=0):
    # Handle any pending web/DNS traffic, blocking up to timeout_ms. Returns False if no server is running.
    if POLLER is None:
        return False
    uptime_seconds()
    try:
        events = POLLER.poll(timeout_ms)
    except Exception:
        return False
    if events:
        if DNS is not None:
            dns_reply()
        accept_clients()
    if RESTART_PENDING:
        log('Restart requested from web page')
        time.sleep(1)
        machine.reset()
    return True

def accept_clients():
    for _ in range(4):          # don't let a burst of requests starve the LEDs
        try:
            conn, addr = WEB.accept()
        except OSError:
            return
        try:
            conn.settimeout(5)
            handle_http(conn)
        except Exception as e:
            log(f'  Web request error: {e}')
        finally:
            try:
                conn.close()
            except Exception:
                pass
        gc.collect()

def read_request(conn):
    data = b''
    while data.find(b'\r\n\r\n') < 0:
        chunk = conn.recv(512)
        if not chunk:
            break
        data += chunk
        if len(data) > 4096:
            raise ValueError('request headers too large')
    split = data.find(b'\r\n\r\n')
    head = data[:split] if split >= 0 else data
    body = data[split + 4:] if split >= 0 else b''
    lines = head.decode().split('\r\n')
    parts = lines[0].split(' ')
    method = parts[0] if parts else ''
    path = parts[1].split('?')[0] if len(parts) > 1 else '/'
    headers = {}
    for line in lines[1:]:
        if ':' in line:
            k, v = line.split(':', 1)
            headers[k.strip().lower()] = v.strip()
    length = int(headers.get('content-length', 0) or 0)
    if length > 16384:
        raise ValueError('request body too large')
    while len(body) < length:
        chunk = conn.recv(min(1024, length - len(body)))
        if not chunk:
            break
        body += chunk
    return method, path, headers, body

def respond(conn, body, status='200 OK', ctype='application/json', extra=''):
    if isinstance(body, str):
        body = body.encode()
    elif not isinstance(body, bytes):
        body = json.dumps(body).encode()
    head = 'HTTP/1.1 {}\r\nContent-Type: {}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n{}\r\n'
    conn.sendall(head.format(status, ctype, len(body), extra).encode())
    for i in range(0, len(body), 1024):
        conn.sendall(body[i:i + 1024])

def authorized(headers):
    password = CONFIG.get('WEB', {}).get('PASSWORD', '')
    if not password:
        return True
    token = binascii.b2a_base64(('admin:' + password).encode()).decode().strip()
    return headers.get('authorization', '') == 'Basic ' + token

def handle_http(conn):
    global FORCE_REFRESH, RESTART_PENDING
    method, path, headers, body = read_request(conn)
    if not authorized(headers):
        return respond(conn, 'Login required', '401 Unauthorized', 'text/plain',
                       'WWW-Authenticate: Basic realm="LEDWeatherMonitor"\r\n')
    if path in ('/', '/index.html'):
        respond(conn, PAGE_HTML, ctype='text/html; charset=utf-8')
    elif path == '/api/info':
        respond(conn, device_info())
    elif path == '/api/config' and method == 'GET':
        respond(conn, {'config': read_saved_config(), 'base': BASE_CONFIG,
                       'choices': CHOICES, 'restart_keys': RESTART_KEYS})
    elif path == '/api/config' and method == 'POST':
        save_config_from_web(conn, body)
    elif path == '/api/log':
        respond(conn, {'run': Run_Log, 'error': read_text(ERROR_LOG)})
    elif path == '/api/refresh' and method == 'POST':
        FORCE_REFRESH = True
        respond(conn, {'ok': True})
    elif path == '/api/restart' and method == 'POST':
        respond(conn, {'ok': True})
        RESTART_PENDING = True
    elif path == '/api/setup-mode' and method == 'POST':
        with open(FORCE_SETUP_FILE, 'w') as f:
            f.write('1')
        respond(conn, {'ok': True})
        RESTART_PENDING = True
    elif AP_MODE:
        # Captive portal: send phones' connectivity checks to the setup page.
        respond(conn, '', '302 Found', 'text/plain', 'Location: http://' + AP.ifconfig()[0] + '/\r\n')
    else:
        respond(conn, {'errors': ['not found']}, '404 Not Found')

def read_text(path, limit=3000):
    try:
        with open(path, 'r') as f:
            return f.read()[-limit:]
    except Exception:
        return ''

def read_saved_config():
    try:
        config = read_config_file()
        merge_defaults(config, BASE_CONFIG)
        return config
    except Exception:
        return copy_value(CONFIG)

def check_types(new, base, path, errors):
    # Validate (and gently coerce) values for keys that exist in BASE_CONFIG. New keys are accepted as-is.
    for key, default in base.items():
        if key not in new:
            continue
        value = new[key]
        name = path + key
        if isinstance(default, dict):
            if isinstance(value, dict):
                check_types(value, default, name + '.', errors)
            else:
                errors.append(name + ' must be a section')
        elif isinstance(default, bool):
            if not isinstance(value, bool):
                errors.append(name + ' must be true or false')
        elif isinstance(default, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(name + ' must be a number')
            elif isinstance(default, int) and value == int(value):
                new[key] = int(value)
        elif isinstance(default, str) and not isinstance(value, str):
            new[key] = str(value)
        if name in CHOICES and new[key] not in CHOICES[name]:
            errors.append(name + ' must be one of ' + ', '.join(CHOICES[name]))

def apply_live(new):
    # Use the new settings right away, except hardware/network ones which wait for a restart.
    global CONFIG
    live = copy_value(new)
    for path in RESTART_KEYS:
        running = get_path(CONFIG, path, MISSING)
        if running is MISSING:
            del_path(live, path)
        else:
            set_path(live, path, copy_value(running))
    CONFIG = live

def save_config_from_web(conn, body):
    global FORCE_REFRESH
    try:
        new = json.loads(body.decode())
        if not isinstance(new, dict):
            raise ValueError('configuration must be a JSON object')
        errors = []
        check_types(new, BASE_CONFIG, '', errors)
        if errors:
            return respond(conn, {'ok': False, 'errors': errors}, '400 Bad Request')
        merge_defaults(new, BASE_CONFIG)
        old = read_saved_config()
        write_config(new)
    except Exception as e:
        return respond(conn, {'ok': False, 'errors': [str(e)]}, '400 Bad Request')
    restart = any(get_path(old, p) != get_path(new, p) for p in RESTART_KEYS)
    restart = restart or any(get_path(CONFIG, p) != get_path(new, p) for p in RESTART_KEYS)
    apply_live(new)
    FORCE_REFRESH = True
    log('Configuration saved from web page')
    respond(conn, {'ok': True, 'restart_required': restart})

def current_ip():
    if AP_MODE:
        return AP.ifconfig()[0]
    return WLAN.ifconfig()[0] if WLAN.active() else '0.0.0.0'

def uptime_seconds():
    global UPTIME_MS, _LAST_TICKS
    now = time.ticks_ms()
    UPTIME_MS += time.ticks_diff(now, _LAST_TICKS)
    _LAST_TICKS = now
    return UPTIME_MS // 1000

def fmt_duration(seconds):
    d, seconds = divmod(int(seconds), 86400)
    h, seconds = divmod(seconds, 3600)
    m = seconds // 60
    return (str(d) + 'd ' if d else '') + str(h) + 'h ' + str(m) + 'm'

def fmt_bytes(n):
    return str(round(n / 1024, 1)) + ' KB'

def device_info():
    info = []
    def add(label, fn):
        try:
            info.append([label, fn()])
        except Exception as e:
            info.append([label, 'n/a'])
    u = os.uname()
    add('Hostname', hostname)
    add('Display type', lambda: CONFIG['HARDWARE'].get('TYPE', 'bar'))
    add('Mode', lambda: 'Setup hotspot (could not join Wi-Fi)' if AP_MODE else 'Connected to Wi-Fi')
    if AP_MODE:
        add('Hotspot name', lambda: AP.config('essid'))
        add('Hotspot clients', lambda: len(AP.status('stations')))
        add('IP address', lambda: AP.ifconfig()[0])
        add('Saved Wi-Fi network', lambda: CONFIG['NETWORK']['SSID'] or '(none)')
    else:
        add('Wi-Fi network', lambda: WLAN.config('essid'))
        add('IP address', lambda: WLAN.ifconfig()[0])
        add('Signal', lambda: str(WLAN.status('rssi')) + ' dBm')
    add('MAC address', lambda: binascii.hexlify((AP if AP_MODE else WLAN).config('mac'), ':').decode())
    add('Board', lambda: u.machine)
    add('Firmware', lambda: u.sysname + ' ' + u.release)
    add('CPU', lambda: str(machine.freq() // 1000000) + ' MHz')
    add('Free memory', lambda: fmt_bytes(gc.mem_free()))
    add('Free flash', lambda: fmt_bytes(os.statvfs('/')[0] * os.statvfs('/')[3]))
    add('Uptime', lambda: fmt_duration(uptime_seconds()))
    def clock_text():
        if time.localtime()[0] < 2024:
            return 'not synced yet'
        t = local_now()
        return '{:04d}-{:02d}-{:02d} {:02d}:{:02d}'.format(t[0], t[1], t[2], t[3], t[4])
    add('Local time', clock_text)
    add('Last forecast', lambda: ('never' if PIN_DATA is None else fmt_duration(time.time() - LAST_REFRESH) + ' ago'))
    add('Code version', lambda: (read_text(VERSION_TRACKER_FILE)[:7] or 'unknown'))
    if CONFIG_ERROR:
        info.append(['Config problem', CONFIG_ERROR])
    add('Last error', lambda: read_text(ERROR_LOG, 300) or 'none')
    return info

PAGE_HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LED Weather Monitor</title><style>
:root{--bg:#f4f5f7;--card:#fff;--sec:#f8f9fb;--fg:#1d2330;--mut:#6b7280;--line:#dde1e7;--acc:#2563eb;--bad:#dc2626;--ok:#15803d}
@media(prefers-color-scheme:dark){:root{--bg:#111418;--card:#1b2027;--sec:#161a20;--fg:#e6e9ee;--mut:#9aa3af;--line:#2d343e;--acc:#60a5fa;--bad:#f87171;--ok:#4ade80}}
*{box-sizing:border-box}[hidden]{display:none!important}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,sans-serif}
main{max-width:780px;margin:auto;padding:16px}h1{font-size:20px;margin:4px 0 16px}h2{font-size:16px;margin:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.head{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}.head h2{margin-right:auto}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:5px 4px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:500;width:40%}td{overflow-wrap:anywhere}
button{font:inherit;padding:7px 12px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.p{background:var(--acc);border-color:var(--acc);color:#fff}button.d{color:var(--bad)}button.s{padding:4px 10px;font-size:13px}
.row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.3fr) auto;gap:4px 10px;align-items:center;padding:5px 0;border-bottom:1px solid var(--line)}
.row label{font:13px ui-monospace,monospace;overflow-wrap:anywhere}
.row input:not([type=checkbox]),.row select,.add input,.add select{width:100%;min-width:0;padding:6px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
.row input[type=checkbox]{justify-self:start;width:18px;height:18px;margin:6px 0}
details.sec{border:1px solid var(--line);border-radius:8px;margin:10px 0;background:var(--sec)}
details.sec details.sec{background:var(--card)}details.sec details.sec details.sec{background:var(--sec)}
summary{cursor:pointer;padding:9px 12px;display:flex;gap:8px;align-items:center;list-style:none;font-weight:600;user-select:none}
summary::-webkit-details-marker{display:none}
.chev{color:var(--mut);display:inline-block;width:12px;transition:transform .15s}details[open]>summary .chev{transform:rotate(90deg)}
.cnt{color:var(--mut);font-weight:400;font-size:13px}summary .d{margin-left:auto}
.grp{padding:0 12px 10px}#cfg>.grp{padding:0}
.x{padding:2px 8px;font-size:12px}.r{color:var(--acc);font-size:13px;margin-left:4px;cursor:help}
.addbtn{margin-top:8px;font-size:13px;padding:4px 10px;color:var(--acc);border-style:dashed;background:transparent}
.add{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr) auto auto;gap:8px;margin-top:8px}
.hint{color:var(--mut);font-size:13px;margin:12px 0 0}
.bar{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}#dirty{color:var(--mut);font-size:13px}
#msg{margin-top:10px;min-height:1.4em}.err{color:var(--bad)}.ok{color:var(--ok)}
pre{white-space:pre-wrap;font-size:12px;max-height:360px;overflow:auto;background:var(--bg);padding:8px;border-radius:6px}
@media(max-width:560px){.row{grid-template-columns:minmax(0,1fr) auto}.row label{grid-column:1/-1}.add{grid-template-columns:1fr 1fr}}
</style></head><body><main><h1>LED Weather Monitor</h1>
<div class="card"><div class="head"><h2>Device</h2></div><table id="info"><tr><td>Loading…</td></tr></table>
<div class="bar"><button onclick="act('/api/refresh','Forecast refresh requested.')">Refresh forecast now</button>
<button onclick="restart()">Restart</button><button onclick="restart('/api/setup-mode')">Restart into setup hotspot</button></div></div>
<div class="card"><div class="head"><h2>Configuration</h2><button class="s" onclick="setAll(true)">Expand all</button><button class="s" onclick="setAll(false)">Collapse all</button></div>
<div id="cfg">Loading…</div>
<p class="hint"><span class="r">↻</span> takes effect after a restart. On a section it applies to everything inside it.</p>
<div class="bar"><button class="p" onclick="save(false)">Save</button><button onclick="save(true)">Save &amp; restart</button>
<button onclick="discard()">Discard changes</button><span id="dirty"></span></div><div id="msg"></div></div>
<div class="card"><div class="head"><h2>Logs</h2><button class="s" onclick="loadLogs()">Load logs</button></div><pre id="log" hidden></pre></div>
</main><script>
const $=s=>document.querySelector(s);let cfg={},base={},choices={},rk=[],host='',dirty=false;const closed=new Set();
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]);
const isObj=v=>v!==null&&typeof v=='object'&&!Array.isArray(v);
function el(t,a,...kids){const e=document.createElement(t);Object.assign(e,a||{});for(const k of kids)e.append(k);return e}
async function api(p,o){try{const r=await fetch(p,o);const j=await r.json();if(!r.ok&&!j.errors)j.errors=['HTTP '+r.status];return j}catch(e){return{errors:[String(e)]}}}
function msg(t,c){const m=$('#msg');m.textContent=t;m.className=c||''}
function setDirty(v){dirty=v;$('#dirty').textContent=v?'Unsaved changes':''}
window.onbeforeunload=e=>{if(dirty){e.preventDefault();e.returnValue=''}};
async function loadInfo(){const d=await api('/api/info');if(!Array.isArray(d))return;
$('#info').innerHTML=d.map(([k,v])=>'<tr><th>'+esc(k)+'</th><td>'+esc(v)+'</td></tr>').join('');const h=d.find(x=>x[0]=='Hostname');if(h)host=h[1]}
async function loadConfig(){const d=await api('/api/config');if(!d.config){$('#cfg').textContent='Failed to load: '+(d.errors||[]).join(', ');return}
cfg=d.config;base=d.base;choices=d.choices;rk=d.restart_keys;setDirty(false);render();msg('')}
function discard(){if(!dirty||confirm('Discard unsaved changes?'))loadConfig()}
function render(){$('#cfg').replaceChildren(group(cfg,base,''))}
function setAll(open){document.querySelectorAll('#cfg details').forEach(d=>d.open=open)}
// settings first, then sub-sections, each alphabetical
function keysOf(o){return Object.keys(o).sort((a,b)=>(isObj(o[a])-isObj(o[b]))||a.localeCompare(b))}
function badge(){return el('span',{className:'r',textContent:'↻',title:'Takes effect after a restart'})}
function group(o,b,path){const box=el('div',{className:'grp'});
for(const k of keysOf(o)){const p=path+k,inBase=isObj(b)&&k in b;
box.append(isObj(o[k])?section(o,k,p,inBase?b[k]:null,inBase):field(o,k,p,inBase))}
box.append(adder(o,path));return box}
function section(o,k,p,b,inBase){const d=el('details',{className:'sec',open:!closed.has(p)});
d.ontoggle=()=>{d.open?closed.delete(p):closed.add(p)};const n=Object.keys(o[k]).length;
const s=el('summary',{},el('span',{className:'chev',textContent:'▸'}),el('span',{textContent:k}),el('span',{className:'cnt',textContent:'('+n+')'}));
if(rk.includes(p))s.append(badge());if(!inBase)s.append(delBtn(o,k,p));
d.append(s,group(o[k],b,p+'.'));return d}
function delBtn(o,k,p){return el('button',{className:'x d',textContent:'Remove',title:'Remove '+p,onclick:e=>{e.preventDefault();e.stopPropagation();
if(isObj(o[k])&&Object.keys(o[k]).length&&!confirm('Remove '+p+' and everything in it?'))return;delete o[k];setDirty(true);render()}})}
function field(o,k,p,inBase){const v=o[k],set=x=>{o[k]=x;setDirty(true)};let inp;
if(choices[p]){inp=el('select');const opts=choices[p].includes(v)?choices[p]:[v,...choices[p]];
for(const c of opts)inp.append(el('option',{value:c,textContent:c,selected:c==v}));inp.onchange=()=>set(inp.value)}
else if(typeof v=='boolean'){inp=el('input',{type:'checkbox',checked:v});inp.onchange=()=>set(inp.checked)}
else if(typeof v=='number'){inp=el('input',{type:'number',step:'any',value:v});inp.oninput=()=>{if(inp.value!=='')set(Number(inp.value))}}
else if(Array.isArray(v)){inp=el('input',{value:JSON.stringify(v)});inp.oninput=()=>{try{set(JSON.parse(inp.value));inp.style.outline=''}catch(e){inp.style.outline='2px solid var(--bad)'}}}
else{const s=/PSK|PASSWORD/i.test(k);inp=el('input',{type:s?'password':'text',value:v==null?'':v});
if(s){inp.onfocus=()=>inp.type='text';inp.onblur=()=>inp.type='password'}inp.oninput=()=>set(inp.value)}
inp.id='f_'+p;const lab=el('label',{textContent:k,htmlFor:inp.id,title:p});if(rk.includes(p))lab.append(badge());
return el('div',{className:'row'},lab,inp,inBase?el('span'):delBtn(o,k,p))}
function adder(o,path){const where=path?path.slice(0,-1):'top level';
const open=el('button',{className:'addbtn',textContent:'+ Add to '+where});
const n=el('input',{placeholder:'NEW_SETTING_NAME'}),t=el('select');
for(const x of ['text','number','true/false','section'])t.append(el('option',{textContent:x}));
const add=el('button',{className:'p',textContent:'Add',onclick:()=>{const k=n.value.trim();
if(!k)return msg('Enter a name for the new setting','err');if(k.includes('.'))return msg('Names cannot contain "."','err');
if(k in o)return msg(path+k+' already exists','err');
o[k]={text:'',number:0,'true/false':false,section:{}}[t.value];closed.delete(path+k);setDirty(true);render();msg('Added '+path+k+' (not saved yet)')}});
const form=el('div',{className:'add',hidden:true},n,t,add,el('button',{textContent:'Cancel',onclick:()=>{form.hidden=true;open.hidden=false}}));
open.onclick=()=>{form.hidden=false;open.hidden=true;n.focus()};n.onkeydown=e=>{if(e.key=='Enter')add.click()};
return el('div',{},open,form)}
async function save(andRestart){msg('Saving…');const d=await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
if(!d.ok)return msg('Not saved: '+(d.errors||[]).join('; '),'err');setDirty(false);
if(andRestart)return restart(null,true);
msg(d.restart_required?'Saved. Some changes take effect after a restart.':'Saved and applied.','ok');loadInfo()}
async function restart(path,skip){if(!skip&&!confirm('Restart the device?'))return;await api(path||'/api/restart',{method:'POST'});setDirty(false);
msg('Restarting… this page reloads in 20 seconds. If the device changed networks, find it at http://'+host+'.local/','ok');setTimeout(()=>location.reload(),20000)}
async function act(p,t){const d=await api(p,{method:'POST'});msg(d.ok?t:'Failed: '+(d.errors||[]).join('; '),d.ok?'ok':'err')}
async function loadLogs(){const d=await api('/api/log');const l=$('#log');l.hidden=false;l.textContent=(d.error?'--- error.log ---\n'+d.error+'\n\n':'')+'--- run log ---\n'+(d.run||'')}
loadInfo();loadConfig();setInterval(loadInfo,15000);
</script></body></html>'''

def run_setup_mode():
    # Broadcast the hotspot and serve the config page. Never returns:
    # periodically retries the saved Wi-Fi and restarts into normal mode once it works.
    start_access_point()
    start_web_server()
    start_dns()
    set_LEDs(color='magenta', brightness=5)
    retry_ms = CONFIG['NETWORK'].get('AP_RETRY_MINUTES', 10) * 60000
    last_try = time.ticks_ms()
    while True:
        wait(5)
        if retry_ms <= 0 or not CONFIG['NETWORK']['SSID']:
            continue
        if time.ticks_diff(time.ticks_ms(), last_try) < retry_ms:
            continue
        last_try = time.ticks_ms()
        try:
            clients = len(AP.status('stations'))
        except Exception:
            clients = 0
        if clients:
            log('Someone is connected to the setup hotspot, not retrying Wi-Fi yet')
            continue
        log('Retrying saved Wi-Fi network from setup mode')
        if manage_wifi('connect', useLEDs=False):
            log('Wi-Fi is back, restarting into normal mode')
            time.sleep(1)
            machine.reset()
        WLAN.active(False)
        set_LEDs(color='magenta', brightness=5)

def refresh_due():
    return FORCE_REFRESH or PIN_DATA is None or time.time() - LAST_REFRESH >= CONFIG['TIME_BETWEEN_CHECKS_M'] * 60

def main_loop(initialized, clock):
    global PIN_DATA, LAST_REFRESH, FORCE_REFRESH
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
        FORCE_REFRESH = False
        manage_wifi('connect', useLEDs=useLEDs)
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
    global CONFIG_ERROR
    print('Starting up....')
    initialized = False
    load_config()
    clock = is_clock()
    try:
        init_neopixel()
        set_LEDs(color='cyan', brightness=10)
        if clock:
            generate_clock_map()
        else:
            generate_bar_hours_map()
    except Exception as e:
        # Keep going so the web page is reachable to fix the setting.
        CONFIG_ERROR = 'LED setup failed: ' + str(e)
        log(CONFIG_ERROR)
        write_error_log(CONFIG_ERROR)
    set_hostname()
    try:
        AP.active(False)
    except Exception:
        pass
    force_setup = FORCE_SETUP_FILE in os.listdir()
    if force_setup:
        os.remove(FORCE_SETUP_FILE)
        log('Setup hotspot requested from web page')
    if force_setup or not manage_wifi('connect'):
        run_setup_mode()        # never returns
    start_web_server()
    if CONFIG_ERROR:
        log('Not driving LEDs until the configuration is fixed on the web page')
        while True:
            wait(60)
    while True:
        main_loop(initialized, clock)
        delay()
        initialized = True

main()
