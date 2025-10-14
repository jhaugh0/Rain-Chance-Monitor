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

print('Getting config from local file')
with open(CONFIG_FILE, 'r') as f:
    CONFIG = json.load(f)
print('  >> Loaded!')

RTC = machine.RTC()
WLAN = network.WLAN(network.STA_IF)
ACCUWEATHER_LOCATION_KEY = ''

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
        if HOUR != CONFIG['LED']['ON_HOUR']:
            log('  >> Not the ON_HOUR, skipping update check')
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

class WeatherAPI():
    def __init__(self):
        self.api_key = CONFIG['WEATHERAPI_API_KEY']
    def get_forecast(self):
        log('Getting Weather Data from weatherapi')
        url = "http://api.weatherapi.com/v1/forecast.json"
        url = url + "?key=" + self.api_key + "&q=" + CONFIG['LOCATION']['LATITUDE'] + "," + CONFIG['LOCATION']['LONGITUDE']
        url = url + "&days=1" + "&aqi=no" + "&alerts=no" + "&hour_fields=chance_of_rain,will_it_rain,feelslike_f"
        request = r.get(url)
        localtime = request.json()['location']['localtime']
        forecast = request.json()['forecast']['forecastday'][0]['hour']
        return forecast
    def map_hours_data(self, forecast):
        log('Mapping weatherapi hour data')
        hours = {}
        for hourData in forecast:
            hour = int(hourData['time'].split(' ')[1].split(':')[0])
            hours[hour] = {}
            hours[hour]['rain'] = hourData['chance_of_rain']
            hours[hour]['temp'] = hourData['temp_f']
        log(f'Returned data: {hours}')
        return hours
    def main(self):
        forecast = self.get_forecast()
        hourMap = self.map_hours_data(forecast)
        return hourMap

class Accuweather():
    def __init__(self):
        self.api_key = CONFIG['ACCUWEATHER_API_KEY']
    def get_location_key(self):
        global ACCUWEATHER_LOCATION_KEY
        if ACCUWEATHER_LOCATION_KEY != '':
            return
        log('Getting Accuweather location key')
        url = 'http://dataservice.accuweather.com/locations/v1/cities/geoposition/search?'
        url = url + '&apikey=' + self.api_key
        url = url + '&q=' + CONFIG['LOCATION']['LATITUDE'] + '%2C' + CONFIG['LOCATION']['LONGITUDE']
        response = make_network_request_with_retry(url, 'Failed to get weather key')
        key = str(response['Key'])
        ACCUWEATHER_LOCATION_KEY = key
    def get_data(self):
        log('Getting Weather Data')
        url = 'http://dataservice.accuweather.com/forecasts/v1/hourly/12hour/' + ACCUWEATHER_LOCATION_KEY + '?'
        url = url + '&apikey=' + CONFIG['ACCUWEATHER_API_KEY']
        response = make_network_request_with_retry(url, 'Failed to get weather data')
        return response
    def extract_precip_chance(self, rJson):
        log('Extracting precip chance from response')
        hours = {}
        for hourData in rJson:
            hour = int(hourData['DateTime'].split('T')[1].split(':')[0])
            hours[hour] = {}
            hours[hour]['rain'] = hourData['PrecipitationProbability']
        log(f'Returned data: {hours}')
        return hours
    def main(self):
        self.get_location_key()
        weatherJSON = self.get_data()
        hourMap = self.extract_precip_chance(weatherJSON)
        return hourMap

class WeatherGOV():
    def __init__(self):
        log('Getting Weather Data from weather.gov')
        self.base = 'https://api.weather.gov'
        self.latitude = str(round(float(CONFIG['LOCATION']['LATITUDE']), 4))
        self.longitude = str(round(float(CONFIG['LOCATION']['LONGITUDE']), 4))
        self.headers = {'user-agent':'jordan@haugh.one'}
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

class Delay():
    def get_seconds_to_next_hour(self):
        minute_now = time.localtime()[4]
        second_now = time.localtime()[5]
        current = (minute_now * 60) + second_now
        return 3600 - current
    def sleep_until_next_hour(self):
        delayTime = self.get_seconds_to_next_hour()
        log(f'Will run again in {round(delayTime/60)} minutes, {delayTime%60} seconds')
        time.sleep(delayTime)
    def overnight_sleep(self):
        seconds_to_on_time = (24 - CONFIG['LED']['OFF_HOUR'] + CONFIG['LED']['ON_HOUR']) * 60 * 60
        clock_drift_adjustment = round(seconds_to_on_time * .95)
        log(f'Will run again in {round(clock_drift_adjustment/60)} minutes, {clock_drift_adjustment%60} seconds')
        set_LEDs(color='off')
        manage_wifi(action='disconnect', useLEDs=False)
        time.sleep(clock_drift_adjustment)
        manage_wifi(action='connect', useLEDs=False)
        validate_internet_connection()
        update_RTC()
        self.sleep_until_next_hour()

def init_neopixel():
    log('Initializing NeoPixel Variables')
    global NP
    NP = {}
    rain_pin = machine.Pin(CONFIG['LED']['RAIN_GPIO_PIN'], machine.Pin.OUT)
    NP['RAIN'] = NeoPixel(rain_pin, CONFIG['LED']['TOTAL_COUNT'])
    if CONFIG['LED']['TEMP_STRIP']:
        temp_pin = machine.Pin(CONFIG['LED']['TEMP_GPIO_PIN'], machine.Pin.OUT)
        NP['TEMP'] = NeoPixel(temp_pin, CONFIG['LED']['TOTAL_COUNT'])

def write_user_config(config):
    with open(CONFIG_FILE, 'w') as f:
        f.write(json.dumps(config))

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

def get_local_timeapi_time():
    log('Getting local time from timeapi')
    url = 'https://timeapi.io/api/time/current/coordinate'
    url = url + '?latitude=' + CONFIG['LOCATION']['LATITUDE']
    url = url + '&longitude=' + CONFIG['LOCATION']['LONGITUDE']
    timeResponse = make_network_request_with_retry(url, 'Failed to get time')
    return timeResponse

def get_local_worldtimeapi_time():
    log('Getting local time from worldtimeapi')
    url = 'http://worldtimeapi.org/api/timezone/'
    url = url + CONFIG['LOCATION']['TIME_REGION']
    response = make_network_request_with_retry(url, message='Failed to get time')
    if response:
        global HOUR, DAY
        log(f'  >> Local time {response['datetime']} returned')
        HOUR = int(response['datetime'].split('T')[1].split(":")[0])
        DAY = int(response['datetime'].split('T')[0].split("-")[2])

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
        log('Connecting WiFi')
        log(f'  Using wifi info:\n    SSID: {CONFIG['NETWORK']['SSID']}\n    PSK:  {CONFIG['NETWORK']['PSK']}')
        if not WLAN.isconnected():
            if useLEDs:
                set_LEDs(color='off')
            WLAN.active(True)
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
        WLAN.disconnect()
        log('Disabling WiFi')
        WLAN.active(False)

def validate_internet_connection(tries_before_reconnect=10, max_tries=20):
    log('Validating public internet connection')
    retries = 0
    while True:
        try:
            response = r.get('https://ip.me')
            if response.status_code == 200:
                log(f'  Internet appears to be connected, Public IP: {response.text.strip()}')
                set_LEDs(color='blue', brightness=1)
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

def update_RTC():
    log('Updating RTC time')
    log("  Local time before synchronization：%s" %str(time.localtime()))
    retries = 0
    while retries < CONFIG['NETWORK']['MAX_REQUEST_RETRIES']:
        try:
            ntptime.settime()
            log("  Local time after synchronization：%s" %str(time.localtime()))
            return
        except:
            log(f'  Failed to get NTP time, retry {retries+1}/{CONFIG['NETWORK']['MAX_REQUEST_RETRIES']}')
            retries = retries + 1
            log(f'  Pausing {CONFIG['NETWORK']['REQUEST_RETRY_DELAY_SECONDS']} seconds before next attempt')
        if retries == CONFIG['NETWORK']['MAX_REQUEST_RETRIES']:
            return None
    #RTC.datetime(get_current_time_in_RTC())

def set_LEDs(strip=None, pinMap={}, color='', brightness=50, RGBValue=(0,0,0), startPin=None, blueRed=False, greenRed=False):
    if brightness > 100:
        brightness = 100   
    log(f'Setting LEDs. Brightness: {brightness}')

    def get_color(color, brightness=brightness):
        brightness = round(255 * (brightness * .01))
        def get_percentage(value):
            return round(brightness * value)
        colors = {
            'red':          (brightness,          0,                 0                 ),
            'green':        (0,                   brightness,        0                 ),
            'blue':         (0,                   0,                 brightness        ),
            'yellow':       (brightness,          brightness,        0                 ),
            'cyan':         (0,                   brightness,        brightness        ),
            'white':        (brightness,          brightness,        brightness        ),
            'off':          (0,                   0,                 0                 ),
            'blueRed_30':   (0,                   0,                 get_percentage(1) ),
            'blueRed_40':   (get_percentage(.1), get_percentage(.2), get_percentage(.9)),
            'blueRed_50':   (get_percentage(.2), get_percentage(.3), get_percentage(.5)),
            'blueRed_60':   (get_percentage(.3), get_percentage(.5), get_percentage(.3)),
            'blueRed_70':   (get_percentage(.5), get_percentage(.3), get_percentage(.2)),
            'blueRed_80':   (get_percentage(.9), get_percentage(.2), get_percentage(.1)),
            'blueRed_90':   (get_percentage(1),  0,                  0                 ),
            'greenRed_0':   (0,                  get_percentage(1),  0                 ),
            'greenRed_10':  (get_percentage(.3), get_percentage(.8), 0                 ),
            'greenRed_20':  (get_percentage(.4), get_percentage(.7), 0                 ),
            'greenRed_30':  (get_percentage(.5), get_percentage(.6), 0                 ),
            'greenRed_40':  (get_percentage(.6), get_percentage(.5), 0                 ),
            'greenRed_50':  (get_percentage(.7), get_percentage(.4), 0                 ),
            'greenRed_60':  (get_percentage(.8), get_percentage(.3), 0                 ),
            'greenRed_70':  (get_percentage(.9), get_percentage(.3), 0                 ),
            'greenRed_80':  (get_percentage(.9), get_percentage(.2), 0                 ),
            'greenRed_90':  (get_percentage(.9), get_percentage(.1), 0                 ),
            'greenRed_100': (get_percentage(1),  0,                  0                 ),
        }
        if blueRed:
            rgb = round(color / 10.0) * 10
            index = 'blueRed_' + str(rgb)
            rgbvalue = colors[index] if index in colors else colors['off']
            log(f"      >> indexed to {index} : {str(rgbvalue)}")
            return rgbvalue
        if greenRed:
            rgb = round(color / 10.0) * 10
            index = 'greenRed_' + str(rgb)
            return colors[index] if index in colors else colors['off']
        return colors[color] if color in colors else colors['off']

    def set_all_strips(RGBValue):
        for strip in NP.keys():
            log(f'  Setting strip {strip} to value: {RGBValue}')
            NP[strip].fill(RGBValue)
            NP[strip].write()

    if RGBValue != (0,0,0):
        set_all_strips(RGBValue)
        return
    elif pinMap != {}:
        for hour in pinMap:
            value = pinMap[hour]
            if hour == HOUR:
                log(f'    >> current hour: {hour}, setting brightness to 100%')
                color = get_color(value, brightness=100)
            elif hour < HOUR:
                log(f'    >> Hour {hour} is lower than current: {HOUR}, setting brightness to 25%')
                color = get_color(value, brightness=round(brightness/4))
            else:
                color = get_color(value)
            pinNumber = HOURS_MAP.index(hour)
            log(f'  Setting pin {pinNumber} to color {color} for value {value}')
            strip[pinNumber] = color
    elif startPin is not None:
        log(f'  Setting LED pin {startPin} to: color {get_color(color)}')
        if startPin >= CONFIG['LED']['TOTAL_COUNT']:
            # reset all LEDs
            set_LEDs(color='off')
            return 0
        strip[startPin] = get_color(color)
        strip.write()
        startPin += 1
        return startPin
    else:
        log(f'  Setting all LEDs to {get_color(color)}')
        set_all_strips(get_color(color))
        return
    strip.write()

def create_pin_dict():
    log('Initializing pin dictionary')
    pin_data = {}
    for hour in range(CONFIG['LED']['FIRST_BAR_HOUR'], CONFIG['LED']['FIRST_BAR_HOUR']+CONFIG['LED']['TOTAL_COUNT']):
        pin_data[hour] = None
    return pin_data

def generate_hours_map():
    global HOURS_MAP
    if CONFIG['LED']['CABLE_SIDE'] == 'right':
        HOURS_MAP = list(reversed(range(CONFIG['LED']['FIRST_BAR_HOUR'], CONFIG['LED']['FIRST_BAR_HOUR']+CONFIG['LED']['TOTAL_COUNT'])))
    else:
        HOURS_MAP = list(range(CONFIG['LED']['FIRST_BAR_HOUR'], CONFIG['LED']['FIRST_BAR_HOUR']+CONFIG['LED']['TOTAL_COUNT']))

def map_hours_to_pins():
    pinData = create_pin_dict()
    if CONFIG['PROVIDER'] == 'weatherapi':
        hoursMap = WeatherAPI().main()
    elif CONFIG['PROVIDER'] == 'accuweather':
        hoursMap = Accuweather().main()
    elif CONFIG['PROVIDER'] == 'weathergov':
        hoursMap = WeatherGOV().main()
    for hour in pinData:
        if hour in hoursMap.keys():
            log(f'  Mapping hour {hour} to {hoursMap[hour]}')
            pinData[hour] = hoursMap[hour]
        else:
            log(f'  Mapping hour {hour} to None')
            pinData[hour] = None
    return pinData

def send_map_to_leds(pinData):
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

def main_loop():
    log('Starting Main Loop', initialize=True)
    try:
        manage_wifi('connect')
        validate_internet_connection()
        update_RTC()
        get_local_worldtimeapi_time()
        if HOUR == CONFIG['LED']['OFF_HOUR']:
            Delay().overnight_sleep()
        Check_for_updates().main()
        hourMap = map_hours_to_pins()
        send_map_to_leds(hourMap)
        manage_wifi(action='disconnect')
        return
    except Exception as e:
        write_error_log(str(e))
        log(f'Error occurred: {e}', write_to_file=True)

def main():
    print('Starting up....')
    init_neopixel()
    set_LEDs(color='cyan', brightness=10)
    generate_hours_map()
    while True:
        main_loop()
        Delay().sleep_until_next_hour()

main()
