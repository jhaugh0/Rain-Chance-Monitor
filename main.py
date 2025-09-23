import ntptime
import time
import network
import machine
import os
import json
import urequests as r
from neopixel import NeoPixel

REPO = 'https://github.com/jhaugh0/Rain-Chance-Monitor'
CONFIG_FILE = "config.json"
VERSION_TRACKER_FILE = 'version.txt'
LED = {
    "ON_HOUR"  : 7,
    "OFF_HOUR" : 22,
    "TOTAL_COUNT" : 15,
    "FIRST_HOUR" : 8
}
NETWORK = {
    "MAX_REQUEST_RETRIES" : 5,
    "REQUEST_RETRY_DELAY_SECONDS" : 5,
    "INTERNET_CHECK_RETRY_SECONDS" : 5
}

if os.uname().sysname == 'esp32':
    GPIO_PIN = 35 #M0
elif os.uname().sysname == 'rp2':
    GPIO_PIN = 0
else:
    GPIO_PIN = 0

PIN = machine.Pin(GPIO_PIN, machine.Pin.OUT)
NP = NeoPixel(PIN, LED['TOTAL_COUNT'])
RTC = machine.RTC()
Wlan = network.WLAN(network.STA_IF)
ACCUWEATHER_LOCATION_KEY = ''

def get_user_config():
    import sys
    def read_input(prompt):
        string = sys.stdin.readline()
        return string.strip()
    ssid = read_input('Enter your WiFi SSID: ')
    psk = read_input('Enter your WiFi Password: ')
    api_key = read_input('Enter your Accuweather API Key: ')
    latitude = read_input('Enter your Latitude (e.g. 40.712776): ')
    longitude = read_input('Enter your Longitude (e.g. -74.005974): ')
    cable_side = read_input('Which side is the cable coming out of? enter \'right\' or \'left\'')
    config = {
        'SSID' : ssid,
        'PSK' : psk,
        'ACCUWEATHER_API_KEY' : api_key,
        'LATITUDE' : latitude,
        'LONGITUDE' : longitude,
        'CABLE_SIDE' : cable_side
    }
    with open(CONFIG_FILE, 'w') as f:
        f.write(json.dumps(config))

def get_github_version_hash():
    try:
        print('Trying to get latest github repo version hash')
        response = r.get('https://api.github.com/repos/jhaugh0/Rain-Chance-Monitor/branches/main', headers={'user-agent':os.uname().sysname})
        version = response.json()['commit']['sha']
        print(f'  >> {version} retrieved')
        return version
    except Exception as e:
        print(f'  >> Failed. Error: {e}')
        return None

def get_latest_version():
    print('Getting latest version of main.py')
    request = r.get('https://raw.githubusercontent.com/jhaugh0/Rain-Chance-Monitor/refs/heads/main/main.py', headers={'user-agent':os.uname().sysname})
    if request.status_code == 200:
        print(f'Request succeeded, new file is {len(request.content)} long')
        return request.content

def write_new_version(file, version):
    print('Writing new version file')
    with open(VERSION_TRACKER_FILE, 'w') as f:
        f.write(version)
    print('Writing new main.py')
    with open('main.py', 'w') as f:
        f.write(file)

def check_for_updates():
    if VERSION_TRACKER_FILE in os.listdir():
        try:
            with open(VERSION_TRACKER_FILE, 'r') as f:
                version = f.read()
            print(f'version {version} found on disk')
        except Exception as e:
            print(f'Failed to read version file. error: {e}')
            return
    else:
        version = '0'
    latest_version = get_github_version_hash()
    if latest_version == '':
        print('Failed getting newest version hash')
        return
    if version != latest_version:
        content = get_latest_version()
        write_new_version(file=content, version=latest_version)
        print('Resetting')
        machine.reset()

def get_local_config():
    print('Getting config from local file')
    global NETWORK
    global ACCUWEATHER_API_KEY
    global LATITUDE
    global LONGITUDE
    global TIME_REGION
    with open(CONFIG_FILE, 'r') as f:
        config = json.load(f)
    NETWORK['SSID'] = config['SSID']
    NETWORK['PSK'] = config['PSK']
    ACCUWEATHER_API_KEY = config['ACCUWEATHER_API_KEY']
    LATITUDE = config['LATITUDE']
    LONGITUDE = config['LONGITUDE']
    LED['CABLE_SIDE'] = config['CABLE_SIDE']
    TIME_REGION = config['TIME_REGION']
    print('  >> Loaded!')

def make_network_request_with_retry(url, message):
    print(f'  Making GET request to {url}')
    retries = 0
    while retries < NETWORK['MAX_REQUEST_RETRIES']:
        try:
            response = r.get(url)
            return response.json()
        except:
            print(f'  {message}, retry {retries}/{NETWORK['MAX_REQUEST_RETRIES']}')
            retries = retries + 1
            print(f'  Pausing {NETWORK['REQUEST_RETRY_DELAY_SECONDS']} seconds before next attempt')
            time.sleep(NETWORK['REQUEST_RETRY_DELAY_SECONDS'])
        print(f'  >> status code: {response.status_code}')
        if retries == NETWORK['MAX_REQUEST_RETRIES']:
            return None

def get_local_timeapi_time():
    print('Getting local time from timeapi')
    url = 'https://timeapi.io/api/time/current/coordinate'
    url = url + '?latitude=' + LATITUDE
    url = url + '&longitude=' + LONGITUDE
    timeResponse = make_network_request_with_retry(url, 'Failed to get time')
    return timeResponse

def get_local_worldtimeapi_time():
    print('Getting local time from worldtimeapi')
    url = 'http://worldtimeapi.org/api/timezone/'
    url = url + TIME_REGION
    response = make_network_request_with_retry(url, message='Failed to get time')
    if response:
        print(f'Local time {response['datetime']} returned')
        hour = response['datetime'].split('T')[1].split(":")[0]
        return int(hour)
    return 0

def get_current_time_in_RTC():
    now = time.localtime(time.time() - 14400)
    modified = (
        now[0],
        now[1],
        now[2],
        now[6],
        now[3],
        now[4],
        now[5],
        0
    )  
    return modified

def manage_wifi(action='connect'):
    if action == 'connect':
        print('Connecting WiFi')
        if not Wlan.isconnected():
            set_LEDs(color='off')
            Wlan.active(True)
            Wlan.connect(NETWORK['SSID'], NETWORK['PSK'])
            if os.uname().sysname == 'rp2':
                print('Disabling rp2 specific WiFi power saving settings')
                Wlan.config(pm = 0xa11140)
            start_pin = 0
            while True:
                print(f'    IP: {Wlan.ifconfig()[0]}')
                if Wlan.ifconfig()[0] == '0.0.0.0':
                    start_pin = set_LEDs(loading=True, startPin=start_pin, brightness=5)
                    time.sleep(1)
                else:
                    print('Connected!')
                    set_LEDs(color='white', brightness=1)
                    break
        else:
            print(f"Already connected to wifi: {str(Wlan.ifconfig())}")
            return
    elif action == 'disconnect':
        print('Disconnecting WiFi')
        Wlan.disconnect()
        print('Disabling WiFi')
        Wlan.active(False)

def validate_internet_connection(tries_before_reconnect = 10, max_tries=20):
    print('Validating public internet connection')
    retries = 0
    while True:
        try:
            response = r.get('https://ip.me')
            if response.status_code == 200:
                print(f'  Internet appears to be connected, Public IP: {response.text.strip()}')
                set_LEDs(color='blue', brightness=1)
                return True
        except Exception as e:
            if retries % tries_before_reconnect == 0:
                print(f'  Internet connection not functional yet. Retry #{retries}. Reconnecting wifi to troubleshoot')
                manage_wifi('disconnect')
                print('Delaying 20 seconds')
                time.sleep(20)
                manage_wifi('connect')
            elif retries == max_tries:
                print(f'  Internet connection not functional yet. Hit max retry count of {max_tries}')
                return False
            else:
                print(f'  Internet connection not functional yet. Retry #{retries}. Trying again in {NETWORK['INTERNET_CHECK_RETRY_SECONDS']} seconds')
                print(f'Error: {e}')
            time.sleep(NETWORK['INTERNET_CHECK_RETRY_SECONDS'])
            retries = retries + 1

def update_RTC():
    print('Updating RTC time')
    print("  Local time before synchronization：%s" %str(time.localtime()))
    retries = 0
    while retries < NETWORK['MAX_REQUEST_RETRIES']:
        try:
            ntptime.settime()
            print("  Local time after synchronization：%s" %str(time.localtime()))
            return
        except:
            print(f'  Failed to get NTP time, retry {retries+1}/{NETWORK['MAX_REQUEST_RETRIES']}')
            retries = retries + 1
            print(f'  Pausing {NETWORK['REQUEST_RETRY_DELAY_SECONDS']} seconds before next attempt')
        if retries == NETWORK['MAX_REQUEST_RETRIES']:
            return None
    #RTC.datetime(get_current_time_in_RTC())

def get_accuweather_key():
    global ACCUWEATHER_LOCATION_KEY
    if ACCUWEATHER_LOCATION_KEY != '':
        return
    print('Getting Accuweather location key')
    url = 'http://dataservice.accuweather.com/locations/v1/cities/geoposition/search?'
    url = url + '&apikey=' + ACCUWEATHER_API_KEY
    url = url + '&q=' + LATITUDE + '%2C' + LONGITUDE
    response = make_network_request_with_retry(url, 'Failed to get weather key')
    key = str(response['Key'])
    ACCUWEATHER_LOCATION_KEY = key

def get_accuweather_data():
    print('Getting Weather Data')
    url = 'http://dataservice.accuweather.com/forecasts/v1/hourly/12hour/' + ACCUWEATHER_LOCATION_KEY + '?'
    url = url + '&apikey=' + ACCUWEATHER_API_KEY
    response = make_network_request_with_retry(url, 'Failed to get weather data')
    return response

def set_LEDs(hoursMap={}, multi=False, brightness=50, color='', loading=False, startPin=0):
    def get_color(rain_chance, colors):
        if rain_chance is None:
            return colors['off']
        if rain_chance < 30:
            return colors['green']
        elif rain_chance >= 30 and rain_chance < 55:
            return colors['yellow'] 
        elif rain_chance >= 55:
            return colors['red']
    #idiot check
    if brightness > 100:
        brightness = 100
    brightness = round(255 * (brightness * .01))
    colors = {
        'red' : (brightness,0,0),
        'green' : (0,brightness,0),
        'blue' : (0,0,brightness),
        'yellow' : (brightness,brightness,0),
        'cyan' : (0,brightness,brightness),
        'white' : (brightness, brightness, brightness),
        'off' : (0,0,0)
    }

    if multi:
        for hour in hoursMap:
            value = hoursMap[hour]
            color = get_color(value, colors)
            pinNumber = HOURS_MAP.index(hour)
            print(f'  Setting pin {pinNumber} to color {color} for chance {value}')
            NP[pinNumber] = color
    elif loading:
        if startPin >= LED['TOTAL_COUNT']:
            NP.fill((0,0,0))
            NP.write()
            return 0
        NP[startPin] = colors['cyan']
        NP.write()
        startPin += 1
        return startPin
    else:
        NP.fill(colors[color])
    NP.write()

def extract_precip_chance_from_accuweather(rJson):
    print('Extracting precip chance from response')
    hours = {}
    for hourData in rJson:
        hour = int(hourData['DateTime'].split('T')[1].split(':')[0])
        hours[hour] = hourData['PrecipitationProbability']
    print(f'Returned data: {hours}')
    return hours

def create_pin_dict():
    print('Initializing pin dictionary')
    pin_data = {}
    for hour in range(LED['FIRST_HOUR'], LED['FIRST_HOUR']+LED['TOTAL_COUNT']):
        pin_data[hour] = 0
    return pin_data

def get_seconds_to_next_hour():
    minute_now = time.localtime()[4]
    second_now = time.localtime()[5]
    current = (minute_now * 60) + second_now
    return 3600 - current

def generate_hours_map():
    global HOURS_MAP
    if LED['CABLE_SIDE'] == 'right':
        HOURS_MAP = list(reversed(range(LED['FIRST_HOUR'], LED['FIRST_HOUR']+LED['TOTAL_COUNT'])))
    else:
        HOURS_MAP = list(range(LED['FIRST_HOUR'], LED['FIRST_HOUR']+LED['TOTAL_COUNT']))

def main_loop():
    print('Starting Main Loop')
    manage_wifi('connect')
    validate_internet_connection()
    update_RTC()
    currentHour = get_local_worldtimeapi_time()
    get_accuweather_key()
    if currentHour >= LED['OFF_HOUR'] or currentHour < LED['ON_HOUR']:
        #
        #if localTime['hour'] == LED['OFF_HOUR']:
        #    seconds_to_on_time = (24 - LED['OFF_HOUR'] + LED['ON_HOUR']) * 60 * 60
        #    clock_drift_adjustment = round(seconds_to_on_time * .95)
        #    print(f'Will run again in {round(clock_drift_adjustment/60)} minutes, {clock_drift_adjustment%60} seconds')
        #    time.sleep(clock_drift_adjustment)
        #    manage_wifi(action='connect')
        #    validate_internet_connection()
        #    update_RTC()
        #    localTime = get_local_time()
        print('Turing LEDs off, in dark hour range')
        set_LEDs(color='off')
        manage_wifi(action='disconnect')
        return
    check_for_updates()
    pinData = create_pin_dict()
    weatherJSON = get_accuweather_data()
    hoursMap = extract_precip_chance_from_accuweather(weatherJSON)
    for hour in pinData:
        if hour in hoursMap.keys():
            print(f'  Mapping hour {hour} to chance {hoursMap[hour]}')
            pinData[hour] = hoursMap[hour]
        else:
            print(f'  Mapping hour {hour} to chance None')
            pinData[hour] = None
    print(f'New pin map data: {pinData}')
    set_LEDs(hoursMap=pinData, multi=True, brightness=20)
    manage_wifi(action='disconnect')
    return

def main():
    print('Starting up....')
    set_LEDs(color='cyan', brightness=20)
    get_local_config()
    generate_hours_map()
    while True:
        main_loop()
        delayTime = get_seconds_to_next_hour()
        print(f'Will run again in {round(delayTime/60)} minutes, {delayTime%60} seconds')
        time.sleep(delayTime)

main()