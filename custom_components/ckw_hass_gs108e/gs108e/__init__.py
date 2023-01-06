import requests
from lxml import html
import time

from .netgear_crypt import merge, make_md5

LOGIN_HTM_URL_TMPL = 'http://{ip}/login.htm'
LOGIN_CGI_URL_TMPL = 'http://{ip}/login.cgi'
SWITCH_INFO_HTM_URL_TMPL = 'http://{ip}/switch_info.htm'
PORT_STATISTICS_URL_TMPL = 'http://{ip}/portStatistics.cgi'
ALLOWED_COOKIE_TYPES = ['GS108SID', 'SID']


class GS108Switch(object):
    def __init__(self, host, password):
        self.host = host
        self._password = password
        self._login_password = None
        self.cookie_name = None
        self.cookie_content = None
        self.sleep_time = 0.25
        self.timeout = 15.000
        self.ports = 8
        self._previous_data = {
            'tx': [0] * self.ports,
            'rx': [0] * self.ports,
            'crc': [0] * self.ports,
        }
        self._previous_timestamp = time.perf_counter()
        self._loaded_switch_infos = False
        self._client_hash = None
        self._switch_bootloader = 'unknown'

    def get_unique_id(self):
        return 'gs108_' + self.host.replace('.', '_')

    def get_login_password(self):
        response = requests.get(
            LOGIN_HTM_URL_TMPL.format(ip=self.host),
            allow_redirects=False
        )
        tree = html.fromstring(response.content)
        input_rand_elems = tree.xpath('//input[@id="rand"]')
        user_password = self._password
        if input_rand_elems:
            input_rand = input_rand_elems[0].value
            merged = merge(user_password, input_rand)
            md5 = make_md5(merged)
            return md5
        return user_password

    def get_login_cookie(self):
        if not self._login_password:
            self._login_password = self.get_login_password()
        response = requests.post(
            LOGIN_CGI_URL_TMPL.format(ip=self.host),
            data=dict(password=self._login_password),
            allow_redirects=True
        )
        for ct in ALLOWED_COOKIE_TYPES:
            cookie = response.cookies.get(ct, None)
            if cookie:
                self.cookie_name = ct
                self.cookie_content = cookie
                return True
        tree = html.fromstring(response.content)
        error_msg = tree.xpath('//input[@id="err_msg"]')
        if error_msg:
            print("ERROR get_login_cookie:", error_msg[0].value)
            # 5 Minutes pause
            #time.sleep(5 * 60)
        return False

    def _request(self, method, url, data=None, timeout=15.00, allow_redirects=False):
        if not self.cookie_name or not self.cookie_content:
            return None
        jar = requests.cookies.RequestsCookieJar()
        jar.set(self.cookie_name, self.cookie_content, domain=self.host, path='/')
        request_func = requests.post if method == 'post' else requests.get
        try:
            page = request_func(
                url,
                data=data,
                cookies=jar,
                timeout=timeout,
                allow_redirects=allow_redirects
            )
            return page
        except requests.exceptions.Timeout:
            return None

    def fetch_switch_infos(self):
        url = SWITCH_INFO_HTM_URL_TMPL.format(ip=self.host)
        method = 'get'
        return self._request(url=url, method=method)

    def fetch_port_statistics(self, client_hash):
        url = PORT_STATISTICS_URL_TMPL.format(ip=self.host)
        method = 'post'
        data = None
        if client_hash:
            data = {'hash': client_hash}
        return self._request(url=url, method=method, data=data, allow_redirects=False)

    def _parse_port_statistics(self, tree):
        if self._switch_bootloader in ['V2.06.03']:
            rx = tree.xpath('//input[@name="rxPkt"]')
            tx = tree.xpath('//input[@name="txpkt"]')
            crc = tree.xpath('//input[@name="crcPkt"]')

            # convert to int
            rx = [int(v.value, 16) for v in rx]
            tx = [int(v.value, 16) for v in tx]
            crc = [int(v.value, 16) for v in crc]

        else:
            rx = tree.xpath('//tr[@class="portID"]/td[2]')
            tx = tree.xpath('//tr[@class="portID"]/td[3]')
            crc = tree.xpath('//tr[@class="portID"]/td[4]')

            # convert to int
            try:
                rx = [int(v.text, 10) for v in rx]
                tx = [int(v.text, 10) for v in tx]
                crc = [int(v.text, 10) for v in crc]
            except TypeError:
                rx = [0] * 8
                tx = [0] * 8
                crc = [0] * 8

        return rx, tx, crc

    def get_switch_infos(self):
        switch_data = {}

        if not self._loaded_switch_infos:
            page = self.fetch_switch_infos()
            if not page:
                return None
            tree = html.fromstring(page.content)

            # switch_info.htm:
            switch_serial_number = 'unknown'

            switch_name = tree.xpath('//input[@id="switch_name"]')[0].value

            # Detect Firmware
            switch_firmware = tree.xpath('//table[@id="tbl1"]/tr[6]/td[2]')[0].text
            if switch_firmware is None:
                # Fallback older versions
                switch_firmware = tree.xpath('//table[@id="tbl1"]/tr[4]/td[2]')[0].text

            switch_bootloader_x = tree.xpath('//td[@id="loader"]')
            switch_serial_number_x = tree.xpath('//table[@id="tbl1"]/tr[3]/td[2]')
            client_hash_x = tree.xpath('//input[@id="hash"]')

            if switch_bootloader_x:
                self._switch_bootloader = switch_bootloader_x[0].text
            if switch_serial_number_x:
                switch_serial_number = switch_serial_number_x[0].text
            if client_hash_x:
                self._client_hash = client_hash_x[0].value

            #print("switch_name", switch_name)
            #print("switch_bootloader", switch_bootloader)
            #print("client_hash", client_hash)
            #print("switch_firmware", switch_firmware)
            #print("switch_serial_number", switch_serial_number)

            switch_data.update(**{
                'switch_ip': self.host,
                'switch_name': switch_name,
                'switch_bootloader': self._switch_bootloader,
                'switch_firmware': switch_firmware,
                'switch_serial_number': switch_serial_number,
            })

            # Avoid a second call on next get_switch_infos() call
            self._loaded_switch_infos = True

        # Hold fire
        time.sleep(self.sleep_time)

        page = self.fetch_port_statistics(client_hash=self._client_hash)
        if not page:
            return None

        # init values
        sum_port_traffic_rx = 0
        sum_port_traffic_tx = 0
        sum_port_traffic_crc_err = 0
        sum_port_speed_bps_rx = 0
        sum_port_speed_bps_tx = 0

        _start_time = time.perf_counter()

        # Parse content
        tree = html.fromstring(page.content)
        rx1, tx1, crc1 = self._parse_port_statistics(tree=tree)
        current_data = {
            'rx': rx1,
            'tx': tx1,
            'crc': crc1
        }

        sample_time = _start_time - self._previous_timestamp
        sample_factor = 1 / sample_time
        switch_data['response_time_s'] = sample_time

        for port_number0 in range(self.ports):
            try:
                port_number = port_number0 + 1
                port_traffic_rx = current_data['rx'][port_number0] - self._previous_data['rx'][port_number0]
                port_traffic_tx = current_data['tx'][port_number0] - self._previous_data['tx'][port_number0]
                port_traffic_crc_err = current_data['crc'][port_number0] - self._previous_data['crc'][port_number0]
                port_speed_bps_rx = int(port_traffic_rx * sample_factor)
                port_speed_bps_tx = int(port_traffic_tx * sample_factor)
            except IndexError:
                #print("IndexError at port_number0", port_number0)
                continue

            # Lowpass-Filter
            if port_traffic_rx < 0:
                port_traffic_rx = 0
            if port_traffic_tx < 0:
                port_traffic_tx = 0
            if port_traffic_crc_err < 0:
                port_traffic_crc_err = 0
            if port_speed_bps_rx < 0:
                port_speed_bps_rx = 0
            if port_speed_bps_tx < 0:
                port_speed_bps_tx = 0

            sum_port_traffic_rx += port_traffic_rx
            sum_port_traffic_tx += port_traffic_tx
            sum_port_traffic_crc_err += port_traffic_crc_err
            sum_port_speed_bps_rx += port_speed_bps_rx
            sum_port_speed_bps_tx += port_speed_bps_tx

            to_mbytes = 0.000001

            def _reduce_digits(v):
                return float("{:.2f}".format(round(v * to_mbytes, 2)))

            switch_data[f'port_{port_number}_traffic_rx_mbytes'] = _reduce_digits(port_traffic_rx)
            switch_data[f'port_{port_number}_traffic_tx_mbytes'] = _reduce_digits(port_traffic_tx)
            switch_data[f'port_{port_number}_speed_rx_mbytes'] = _reduce_digits(port_speed_bps_rx)
            switch_data[f'port_{port_number}_speed_tx_mbytes'] = _reduce_digits(port_speed_bps_tx)
            switch_data[f'port_{port_number}_speed_io_mbytes'] = _reduce_digits(port_speed_bps_rx + port_speed_bps_tx)
            switch_data[f'port_{port_number}_crc_errors'] = port_traffic_crc_err

        self._previous_timestamp = time.perf_counter()
        self._previous_data = {
            'rx': current_data['rx'],
            'tx': current_data['tx'],
            'crc': current_data['crc'],
        }

        switch_data['sum_port_traffic_rx'] = sum_port_traffic_rx
        switch_data['sum_port_traffic_tx'] = sum_port_traffic_tx
        switch_data['sum_port_traffic_crc_err'] = sum_port_traffic_crc_err
        switch_data['sum_port_speed_bps_rx'] = sum_port_speed_bps_rx
        switch_data['sum_port_speed_bps_tx'] = sum_port_speed_bps_tx
        switch_data['sum_port_speed_bps_io'] = sum_port_speed_bps_rx + sum_port_speed_bps_tx

        return switch_data
