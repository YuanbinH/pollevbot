import re
import json
import time
import requests
import bs4 as bs
from .urls import *


class Poll(requests.Session):
    """
    A wrapper for a Python requests Session object.
    Encapsulates various authentication protocols used by PollEv.com.
    """

    def __init__(self, username, password, poll_host):
        super().__init__()
        self.username = username
        self.password = password
        self.poll_host = poll_host
        self.headers = {'user-agent': r"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                      r"(KHTML, like Gecko) Chrome/70.0.3538.102 Safari/537.36"}
        self.hashcode = round(time.time() * 1000)
        # Authentication variables for PollEv.
        self.auth_token = None
        self.firehose_token = None
        self.uid = None
        # Checks if a poll has a correct answer.
        self.has_correct_ans = False
        self.correct_ans_index = 0
        # Whenever we make a get, post, or head request, increment the hashcode.
        for func in [self.get, self.post, self.head]:
            func = self.increment_hashcode(func)

    def increment_hashcode(self, func):
        """
        A function wrapper that implements hashcode incrementation.

        When a request is made to PollEv, PollEv does one of two things:
            1. Increment an existing hashcode by 1, or
            2. Create a new hashcode.
        PollEv requires this hashcode for every request made within the domain.
        """

        def wrapped(*args, **kwargs):
            result = func(*args, **kwargs)
            self.hashcode += 1
            return result

        return wrapped

    def reset_hashcode(self):
        """
        Recalculates the hashcode.
        """
        # The value of the hashcode is milliseconds since epoch.
        self.hashcode = round(time.time() * 1000)

    def login_to_myUW(self):
        """
        Logs into myUW. Upon successful login, retrieves and stores the auth token generated by PollEv.

        All PollEverywhere accounts affiliated with UW use a SAML-based Single Sign-On protocol to log in.
        The protocol is as follows:
            1. PollEv sends a SAML request to MyUW
            2. The user logs in to MyUW, and MyUW confirms that the user is registered on their system
            3. MyUW sends a SAML Response to PollEv, authenticating the user.
        After PollEv receives the SAML response, PollEv gives the user an auth token, then resets the hashcode.
        """
        r = self.get(SAML_REQ_URL, headers={'referer': LOGIN_PAGE_URL})
        soup = bs.BeautifulSoup(r.text, "html.parser")
        session_id = re.findall('jsessionid=(.*)\.', soup.find('form', id='idplogindiv').get('action'))
        r = self.post(UW_LOGIN_URL.format(session_id),
                      data={'j_username': self.username, 'j_password': self.password, '_eventId_proceed': 'Sign in'},
                      headers={'referer': UW_LOGIN_URL.format(session_id)})
        # The SAML Response is encoded in a large html document.
        soup = bs.BeautifulSoup(r.text, "html.parser")
        saml_response = soup.find('input', type='hidden')
        if saml_response:
            r = self.post(CALLBACK_URL, headers={'referer': UW_REFERRER_URL, 'origin': UW_HOME_URL},
                          data={'SAMLResponse': saml_response['value']})
            print("Login successful.")
        else:
            exit("Your username/password was incorrect.")

        # PollEv's auth token is encoded in a query string.
        self.auth_token = re.findall('pe_auth_token=(.*)', r.url)[0]
        self.reset_hashcode()
        csrf_token = self.get(CSRF_URL.format(self.hashcode)).json()['token']
        self.post(P_AUTH_URL,
                  headers={'referer': P_AUTH_TOKEN_URL.format(self.auth_token), 'x-csrf-token': csrf_token},
                  data={'token': self.auth_token})


    @staticmethod
    def fake_cookie():
        """
        Generates a fake id cookie (32-digit hex string separated by dashes).

        Helper method for connect_to_channel().
        """
        # String digit format: 8-4-4-4-12
        import secrets
        return str(secrets.token_hex(4)) + '-' + str(secrets.token_hex(2)) + '-' + str(secrets.token_hex(2)) + '-' + \
               str(secrets.token_hex(2)) + '-' + str(secrets.token_hex(6))

    def connect_to_channel(self):
        """
        Given that the user is logged in, retrieve a firehose token.
        If the poll host is not affiliated with UW, PollEv will return a firehose token with a null value.
        """
        # Before issuing a token, AWS checks for two visitor cookies that PollEverywhere generates using js.
        # They are random, dash-separated 32-digit hex codes.
        self.cookies['pollev_visitor'] = self.fake_cookie()
        self.cookies['pollev_visit'] = self.fake_cookie()
        r = self.get(REGISTRATION_URL.format(self.poll_host, self.hashcode),
                     headers={'referer': POLLEV_HOST_URL.format(self.poll_host)})
        self.firehose_token = r.json()['firehose_token']

    def is_open(self, ignore_prev_polls=False):
        """
        Given that the user is logged in and has a firehose token, checks if the poll host
        has any active polls on PollEv. If an active poll exists, retrieves and stores the poll's unique id.
        """
        # PollEv changes its request recipient depending on the organization affiliated with the poll host.
        # Every poll affiliated with UW uses AWS Firehose, so PollEv queries AWS Firehose.
        # Polls not affiliated with any organization are directed to PollEv, and do not require a firehose token.

        # If the poll host has no polls open, Firehose won't respond. PollEv usually won't respond.
        try:
            if self.firehose_token:
                r = self.get(TOKEN_UID_URL.format(self.poll_host, self.firehose_token, self.hashcode), timeout=0.3)
            else:
                r = self.get(NO_TOKEN_UID_URL.format(self.poll_host, self.hashcode), timeout=0.3)
            new_uid = json.loads(r.json()['message'])['uid']
        # If Firehose/PollEv don't respond, requests raises a ReadTimeout exception.
        # PollEv sometimes responds with an empty json message if no polls are open, which raises a KeyError.
        except (requests.exceptions.ReadTimeout, KeyError):
            return False
        if ignore_prev_polls and self.uid == new_uid:
            return False
        else:
            self.uid = new_uid
            return True

    def clear_responses(self):
        """
        Given that the user is logged in and the poll is open, clears any previous responses sent
        by the user.

        Helper method for answer_poll().
        """
        csrf_token = self.get(CSRF_URL.format(self.hashcode)).json()['token']
        r = self.get(RESPONSE_UID_URL.format(self.uid, self.hashcode), headers={'accept': 'application/json'})
        # If the poll is currently unanswered, the response json is an empty list.
        if r.json():
            self.post(POLL_RESULTS_URL.format(r.json()[0]['id']),
                      headers={'x-http-method-override': 'DELETE', 'x-csrf-token': csrf_token})

    def answer_poll(self, clear_responses):
        """
        Given that the user is logged in and the poll is open, submits a response to the poll.
        If the poll host specified a correct option, submit the correct option as a response.
        Otherwise, submit the first option.
        """
        if clear_responses:
            self.clear_responses()
        poll_data = self.get(POLLEV_INFO_URL.format(self.uid, self.hashcode)).json()['multiple_choice_poll']
        poll_options = poll_data['options']
        # The option id is an integer that maps to a possible response to the poll, and increments by 1 for each option.
        # Example: 153525 -> option 1, 153526 -> option 2, 153527 -> option 3, etc.
        option_id = poll_options[0]['id']
        for i, option in enumerate(poll_options):
            # If an option is correct, it will be marked in the json
            if option['correct'] is True:
                self.correct_ans_index = i
                self.has_correct_ans = True
                break
        csrf_token = self.get(CSRF_URL.format(self.hashcode)).json()['token']
        r = self.post(SEND_RESPONSE_URL.format(self.uid, option_id + self.correct_ans_index),
                      headers={'x-csrf-token': csrf_token},
                      data={'accumulator_id': option_id, 'poll_id': poll_data['id'], 'source': 'pollev_page'})

        # Informational terminal output.
        print("\nPoll Title: " + poll_data['title'] + "\n")
        if r.status_code == 422:
            print("Could not submit a response. This could be because:")
            print("\t1. The instructor has locked this poll and is not accepting responses at this time.")
            print("\t2. You have already submitted a response.\n")
        elif self.has_correct_ans:
            print("The correct answer to this question is option " + str(self.correct_ans_index + 1) + ": "
                  + poll_options[self.correct_ans_index]['humanized_value'] + ".")
            print("Successfully selected option " + str(self.correct_ans_index + 1) + "!")
        else:
            print("The instructor did not specify a correct answer for this question. ")
            print("Successfully selected option 1: " + str(poll_options[0]['humanized_value'] + '!'))

    def run(self, delay=5, wait_to_respond=5, clear_responses=False, run_forever=True, ignore_prev_polls=True):
        """
        Runs the script.

        :param delay: Specifies how long the script will wait between queries to check if a poll is open (seconds).
        :param wait_to_respond: Specifies how long the script will wait to respond to an open poll (seconds).
        :param clear_responses: If true, clears any previous responses sent by the user before submitting a response.
        :param run_forever: If true, runs the script forever.
        :param ignore_prev_polls: If true, does not respond to polls that this script has already responded to.
               This parameter should remain set to true, as continuous failed queries will look suspicious
               and may result in an ip ban.
        """
        from itertools import count

        self.login_to_myUW()
        self.connect_to_channel()
        while True:
            try:
                counter = count(1)
                while not self.is_open(ignore_prev_polls=ignore_prev_polls):
                    print("\r" + self.poll_host.capitalize() + " has not opened any new polls. Waiting " + str(delay)
                          + " seconds before checking again. Checked " + str(next(counter)) + " times so far.", end='')
                    time.sleep(delay)
                if wait_to_respond is not 0:
                    print("\n" + self.poll_host.capitalize() + " has opened a new poll! Waiting "
                          + str(wait_to_respond) + " seconds before responding.")
                    time.sleep(wait_to_respond)
                self.answer_poll(clear_responses=clear_responses)
                if not run_forever:
                    break
            # Make sure the bot won't crash when windows Task Scheduler runs it
            except Exception:
                pass