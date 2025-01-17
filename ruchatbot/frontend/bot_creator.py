"""
Хелпер для создания экземпляра бота со всем дефолтным функционалом.
Используется консольным, flask и telegram-вариантами чатбота.

25-10-2020 добавлено автоконфигурирование веб-сервиса читчата
10.01.2021 кроме урла сервиса читчата теперь используется структура с доп. конфигурационными полями (температура etc)
"""

import logging
import requests

from ruchatbot.bot.bot_profile import BotProfile
from ruchatbot.bot.profile_facts_reader import ProfileFactsReader
from ruchatbot.bot.text_utils import TextUtils
from ruchatbot.bot.simple_answering_machine import SimpleAnsweringMachine
from ruchatbot.bot.bot_scripting import BotScripting
from ruchatbot.bot.bot_personality import BotPersonality
from ruchatbot.bot.plain_file_faq_storage import PlainFileFaqStorage
from ruchatbot.scenarios.scenario_who_am_i import Scenario_WhoAmI


class ChitchatConfig:
    """ Конфигурационные параметры сервиса читчата """
    def __init__(self):
        self.service_endpoint = 'http://127.0.0.1:9098'
        self.temperature = 0.9
        self.num_return_sequences = 1

    def build_query_url(self, context):
        s = '{}/reply?context={}&temperature={}'.format(self.service_endpoint, context, self.temperature)
        if self.num_return_sequences > 1:
            s += '&num_return_sequences={}'.format(self.num_return_sequences)

        return s

    def __repr__(self):
        return '{} temperature={} num_return_sequences={}'.format(self.service_endpoint, self.temperature, self.num_return_sequences)


def create_chatbot(profile_path, models_folder, w2v_folder, data_folder, debugging, bot_id='test_bot',
                   chitchat_config=None,
                   enable_verbal_forms=False):
    """Создаем и инициализируем экземпляр чатбота с заданным профилем """

    # NLP pileline: содержит инструменты для работы с текстом, включая морфологию и таблицы словоформ,
    # part-of-speech tagger, NP chunker и прочее.
    text_utils = TextUtils()
    if enable_verbal_forms:
        text_utils.load_embeddings(w2v_dir=w2v_folder, wc2v_dir=models_folder)
    else:
        text_utils.load_embeddings(w2v_dir=w2v_folder, wc2v_dir=None)
    text_utils.load_dictionaries(data_folder, models_folder)

    # Настроечные параметры аватара собраны в профиле - файле в json формате.
    profile = BotProfile()
    profile.load(profile_path, data_folder, models_folder)

    # Контейнер для правил
    scripting = BotScripting(data_folder)
    scripting.load_rules(profile.rules_path, profile.smalltalk_generative_rules, profile.constants, text_utils)

    # Добавляем скрипты на питоне
    scripting.add_scenario(Scenario_WhoAmI())

    # Инициализируем движок вопросно-ответной системы. Он может обслуживать несколько
    # ботов с разными провилями (базами фактов и правил), хотя тут у нас будет работать только один.
    machine = SimpleAnsweringMachine(text_utils=text_utils)
    machine.load_models(scripting.get_rule_paths(), data_folder, models_folder, profile.constants, enable_verbal_forms)
    machine.trace_enabled = debugging

    # Пробуем подцепить локальный сервис читчата
    if chitchat_config is not None and chitchat_config.service_endpoint:
        probe_chitchat_url = chitchat_config.service_endpoint
        try:
            logging.debug('Trying to connect to chit-chat service "%s"...', probe_chitchat_url)
            chitchat_response = requests.get(probe_chitchat_url + '/')
            if chitchat_response.ok:
                machine.chitchat_config = chitchat_config
        except Exception as ex:
            # веб-сервис чит-чата недоступен...
            logging.error('Chit-chat service error: %s', ex)

    # Конкретная реализация хранилища фактов - плоские файлы в utf-8, с минимальным форматированием
    profile_facts = ProfileFactsReader(text_utils=text_utils,
                                       profile_path=profile.premises_path,
                                       constants=profile.constants)

    # Подключаем простое файловое хранилище с FAQ-правилами бота.
    # Движок бота сопоставляет вопрос пользователя с опорными вопросами в FAQ базе,
    # и если нашел хорошее соответствие (синонимичность выше порога), то
    # выдает ответную часть найденной записи.
    faq_storage = PlainFileFaqStorage(profile.faq_path, constants=profile.constants, text_utils=text_utils)

    # Инициализируем аватара
    bot = BotPersonality(bot_id=bot_id,
                         engine=machine,
                         facts=profile_facts,
                         faq=faq_storage,
                         scripting=scripting,
                         profile=profile)

    return bot
