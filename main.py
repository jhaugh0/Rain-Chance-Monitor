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

if os.uname().sysname == 'esp32':
    GPIO_PIN = 35 #M0
elif os.uname().sysname == 'rp2':
    GPIO_PIN = 0
else:
    GPIO_PIN = 0

PIN = machine.Pin(GPIO_PIN, machine.Pin.OUT)
RTC = machine.RTC()
ACCUWEATHER_LOCATION_KEY = ''

class Check_for_updates():
    def __init__(self):
        print('Initializing update checker')
        self.Main_file_url = 'https://raw.githubusercontent.com/' + REPO + '/refs/heads/main/main.py'
        self.Version_hash_url = 'https://api.github.com/repos/' + REPO + '/branches/main'
        self.Request_headers = {'user-agent':os.uname().sysname}
    def get_version_from_disk(self):
        print('Getting version from disk')
        if VERSION_TRACKER_FILE in os.listdir():
            try:
                with open(VERSION_TRACKER_FILE, 'r') as f:
                    version = f.read()
                print(f'  >> {version} found on disk')
            except Exception as e:
                print(f'  >> Failed to read version file. error: {e}')
                return
        else:
            print('  >> No version file found on disk, assuming version 0')
            version = '0'
        return version
    def get_version_from_github(self):
        try:
            print('Trying to get latest github repo version hash')
            response = r.get(self.Version_hash_url, headers=self.Request_headers)
            version = response.json()['commit']['sha']
            print(f'  >> {version} retrieved')
            return version
        except Exception as e:
            print(f'  >> Failed. Error: {e}')
            return ''
    def get_latest_file_version(self):
        print('Getting latest version of main.py')
        request = r.get(self.Main_file_url, headers=self.Request_headers)
        if request.status_code == 200:
            print(f'  >> Request succeeded, new file is {len(request.content)} chars long')
            return request.content
        print('  >> Failed getting latest version of main.py')
        return ''
    def write_new_version(self, file, version):
        print('Writing new version file')
        with open(VERSION_TRACKER_FILE, 'w') as f:
            f.write(version)
        print('Writing new main.py')
        with open('main.py', 'w') as f:
            f.write(file)
    def main(self):
        current_version = self.get_version_from_disk()
        latest_version = self.get_version_from_github()
        if latest_version == '':
            print('Failed getting newest version hash')
            return
        if current_version != latest_version:
            print(f'New version found\n  Current version: {current_version}\n  New version:     {latest_version}')
            content = self.get_latest_file_version()
            if content == '':
                return
            self.write_new_version(file=content, version=latest_version)
            print('Resetting')
            machine.reset()
        else:
            print(f'No new version found\n  Current version: {current_version}\n  Latest version:  {latest_version}')

def write_user_config(config):
    with open(CONFIG_FILE, 'w') as f:
        f.write(json.dumps(config))

def get_weatherdata_api_data():
    print('Getting Weather Data from weatherapi')
    url = "http://api.weatherapi.com/v1/forecast.json"
    url = url + "?key=" + CONFIG['WEATHERAPI_API_KEY'] + "&q=" + CONFIG['LOCATION']['LATITUDE'] + "," + CONFIG['LOCATION']['LONGITUDE']
    url = url + "&days=1" + "&aqi=no" + "&alerts=no" + "&hour_fields=chance_of_rain,will_it_rain,feelslike_f"
    request = r.get(url)
    localtime = request.json()['location']['localtime']
    forecast = request.json()['forecast']['forecastday'][0]['hour']
    return localtime, forecast

def extract_precip_chance_from_weatherapi(forecast):
    print('Mapping weatherapi data')
    hours = {}
    for hourData in forecast:
        hour = int(hourData['time'].split(' ')[1].split(':')[0])
        hours[hour] = hourData['chance_of_rain']
    print(f'Returned data: {hours}')
    return hours

def get_local_config():
    print('Getting config from local file')
    global CONFIG
    with open(CONFIG_FILE, 'r') as f:
        CONFIG = json.load(f)
    print('  >> Loaded!')

def make_network_request_with_retry(url, message):
    print(f'  Making GET request to {url}')
    retries = 0
    while retries < CONFIG['NETWORK']['MAX_REQUEST_RETRIES']:
        try:
            response = r.get(url)
            return response.json()
        except:
            print(f'  {message}, retry {retries}/{CONFIG['NETWORK']['MAX_REQUEST_RETRIES']}')
            retries = retries + 1
            print(f'  Pausing {CONFIG['NETWORK']['REQUEST_RETRY_DELAY_SECONDS']} seconds before next attempt')
            time.sleep(CONFIG['NETWORK']['REQUEST_RETRY_DELAY_SECONDS'])
        print(f'  >> status code: {response.status_code}')
        if retries == CONFIG['NETWORK']['MAX_REQUEST_RETRIES']:
            return None

def get_local_timeapi_time():
    print('Getting local time from timeapi')
    url = 'https://timeapi.io/api/time/current/coordinate'
    url = url + '?latitude=' + CONFIG['LOCATION']['LATITUDE']
    url = url + '&longitude=' + CONFIG['LOCATION']['LONGITUDE']
    timeResponse = make_network_request_with_retry(url, 'Failed to get time')
    return timeResponse

def get_local_worldtimeapi_time():
    print('Getting local time from worldtimeapi')
    url = 'http://worldtimeapi.org/api/timezone/'
    url = url + CONFIG['LOCATION']['TIME_REGION']
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

def manage_wifi(action='connect', useLEDs=True):
    if action == 'connect':
        print('Connecting WiFi')
        if not WLAN.isconnected():
            if useLEDs:
                set_LEDs(color='off')
            WLAN.active(True)
            WLAN.connect(CONFIG['NETWORK']['SSID'], CONFIG['NETWORK']['PSK'])
            if os.uname().sysname == 'rp2':
                print('Disabling rp2 specific WiFi power saving settings')
                Wlan.config(pm = 0xa11140)
            start_pin = 0
            while True:
                print(f'    IP: {WLAN.ifconfig()[0]}')
                if WLAN.ifconfig()[0] == '0.0.0.0':
                    if useLEDs:
                        start_pin = set_LEDs(startPin=start_pin, brightness=5)
                    time.sleep(1)
                else:
                    print('Connected!')
                    if useLEDs:
                        set_LEDs(color='white', brightness=1)
                    break
        else:
            print(f"Already connected to wifi: {str(WLAN.ifconfig())}")
            return
    elif action == 'disconnect':
        print('Disconnecting WiFi')
        WLAN.disconnect()
        print('Disabling WiFi')
        WLAN.active(False)

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
                print(f'  Internet connection not functional yet. Retry #{retries}/{max_tries}. Reconnecting wifi to troubleshoot')
                manage_wifi('disconnect')
                print('Delaying 20 seconds')
                time.sleep(20)
                manage_wifi('connect')
            elif retries == max_tries:
                print(f'  Internet connection not functional yet. Hit max retry count of {max_tries}')
                return False
            else:
                print(f'  Internet connection not functional yet. Retry #{retries}/{max_tries}. Trying again in {CONFIG['NETWORK']['INTERNET_CHECK_RETRY_SECONDS']} seconds')
                print(f'Error: {e}')
            time.sleep(CONFIG['NETWORK']['INTERNET_CHECK_RETRY_SECONDS'])
            retries = retries + 1

def update_RTC():
    print('Updating RTC time')
    print("  Local time before synchronization %s" %str(time.localtime()))
    retries = 0
    while retries < CONFIG['NETWORK']['MAX_REQUEST_RETRIES']:
        try:
            ntptime.settime()
            print("  Local time after synchronization %s" %str(time.localtime()))
            return
        except:
            print(f'  Failed to get NTP time, retry {retries+1}/{CONFIG['NETWORK']['MAX_REQUEST_RETRIES']}')
            retries = retries + 1
            print(f'  Pausing {CONFIG['NETWORK']['REQUEST_RETRY_DELAY_SECONDS']} seconds before next attempt')
        if retries == CONFIG['NETWORK']['MAX_REQUEST_RETRIES']:
            return None
    #RTC.datetime(get_current_time_in_RTC())

def get_accuweather_key():
    if CONFIG['PROVIDER'] != 'accuweather':
        return
    global ACCUWEATHER_LOCATION_KEY
    if ACCUWEATHER_LOCATION_KEY != '':
        return
    print('Getting Accuweather location key')
    url = 'http://dataservice.accuweather.com/locations/v1/cities/geoposition/search?'
    url = url + '&apikey=' + CONFIG['ACCUWEATHER_API_KEY']
    url = url + '&q=' + CONFIG['LOCATION']['LATITUDE'] + '%2C' + CONFIG['LOCATION']['LONGITUDE']
    response = make_network_request_with_retry(url, 'Failed to get weather key')
    key = str(response['Key'])
    ACCUWEATHER_LOCATION_KEY = key

def get_accuweather_data():
    print('Getting Weather Data')
    url = 'http://dataservice.accuweather.com/forecasts/v1/hourly/12hour/' + ACCUWEATHER_LOCATION_KEY + '?'
    url = url + '&apikey=' + CONFIG['ACCUWEATHER_API_KEY']
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
    
    def get_color(rain_chance, colors):
        if rain_chance is None:
            return colors['off']
        if rain_chance < 30:
            return colors['green']
        elif rain_chance >= 30 and rain_chance < 55:
            return colors['yellow'] 
        elif rain_chance >= 55:
            return colors['red']

    if hoursMap:
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
    for hour in range(CONFIG['LED']['FIRST_BAR_HOUR'], CONFIG['LED']['FIRST_BAR_HOUR']+CONFIG['LED']['TOTAL_COUNT']):
        pin_data[hour] = 0
    return pin_data

def get_seconds_to_next_hour():
    minute_now = time.localtime()[4]
    second_now = time.localtime()[5]
    current = (minute_now * 60) + second_now
    return 3600 - current

def sleep_until_next_hour():
    delayTime = get_seconds_to_next_hour()
    print(f'Will run again in {round(delayTime/60)} minutes, {delayTime%60} seconds')
    time.sleep(delayTime)

def generate_hours_map():
    global HOURS_MAP
    if CONFIG['LED']['CABLE_SIDE'] == 'right':
        HOURS_MAP = list(reversed(range(CONFIG['LED']['FIRST_BAR_HOUR'], CONFIG['LED']['FIRST_BAR_HOUR']+CONFIG['LED']['TOTAL_COUNT'])))
    else:
        HOURS_MAP = list(range(CONFIG['LED']['FIRST_BAR_HOUR'], CONFIG['LED']['FIRST_BAR_HOUR']+CONFIG['LED']['TOTAL_COUNT']))

def main_loop():
    print('Starting Main Loop')
    manage_wifi('connect')
    validate_internet_connection()
    update_RTC()
    currentHour = get_local_worldtimeapi_time()
    get_accuweather_key()
    if currentHour == CONFIG['LED']['OFF_HOUR']:
        seconds_to_on_time = (24 - CONFIG['LED']['OFF_HOUR'] + CONFIG['LED']['ON_HOUR']) * 60 * 60
        clock_drift_adjustment = round(seconds_to_on_time * .95)
        print(f'Will run again in {round(clock_drift_adjustment/60)} minutes, {clock_drift_adjustment%60} seconds')
        manage_wifi(action='disconnect', useLEDs=False)
        time.sleep(clock_drift_adjustment)
        manage_wifi(action='connect', useLEDs=False)
        validate_internet_connection()
        update_RTC()
        sleep_until_next_hour()
    Check_for_updates().main()
    pinData = create_pin_dict()
    if CONFIG['PROVIDER'] == 'weatherapi':
        localtime, forecast = get_weatherdata_api_data()
        hoursMap = extract_precip_chance_from_weatherapi(forecast)
    elif CONFIG['PROVIDER'] == 'accuweather':
        weatherJSON = get_accuweather_data()
        hoursMap = extract_precip_chance_from_accuweather(weatherJSON)
    for hour in pinData:
        if hour in hoursMap.keys():
            print(f'  Mapping hour {hour} to chance {hoursMap[hour]}')
            pinData[hour] = hoursMap[hour]
        else:
            print(f'  Mapping hour {hour} to chance None')
            pinData[hour] = None
    set_LEDs(hoursMap=pinData, brightness=20)
    manage_wifi(action='disconnect')
    return

def main():
    global NP, WLAN
    print('Starting up....')
    get_local_config()
    NP = NeoPixel(PIN, CONFIG['LED']['TOTAL_COUNT'])
    WLAN = network.WLAN(network.STA_IF)
    set_LEDs(color='cyan', brightness=20)
    generate_hours_map()
    while True:
        main_loop()
        sleep_until_next_hour()

main()
