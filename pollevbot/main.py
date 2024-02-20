from pollevbot import PollBot


def main():
    user = 'yuanbin@unc.edu'
    password = '2r!aKVdk6DKvN25'
    host = 'tuscanleather011'
    login_type = 'pollev'

    # If you're using a non-uw PollEv account,
    # add the argument "login_type='pollev'"
    with PollBot(user, password, host, login_type) as bot:
        bot.run()


if __name__ == '__main__':
    main()
