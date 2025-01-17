FROM python:3.6-slim

SHELL ["/bin/bash", "-c"]

# Default to UTF-8 file.encoding
ENV LANG C.UTF-8

RUN apt-get update
RUN apt-get install -y python python-pip
RUN apt-get -y install git-core
RUN apt-get install -y liblzma-dev

RUN pip install --upgrade pip
RUN pip install --upgrade setuptools

RUN pip install git+https://github.com/Koziev/rulemma
RUN pip uninstall -y numpy
RUN yes | apt-get install python-numpy

RUN pip install torch==1.7.1
RUN pip install tensorflow==2.4.0
RUN pip install transformers==4.1.1
RUN pip install keras==2.4.3
RUN pip install sentencepiece
RUN pip install lightgbm
RUN pip install scikit-learn==0.24.0
RUN pip install python-Levenshtein
RUN pip install gensim
RUN pip install pathlib
RUN pip install python-crfsuite
RUN pip install colorama
RUN pip install coloredlogs
RUN pip install git+https://www.github.com/keras-team/keras-contrib.git
RUN pip install requests
RUN pip install flask
RUN pip install flask_sqlalchemy
RUN pip install flask_wtf
RUN pip install python-telegram-bot --upgrade
RUN pip install h5py==2.10.0
RUN pip install pyconll
RUN pip install ufal.udpipe

WORKDIR /home
ADD ruword2tags.tar.gz /home
WORKDIR /home/ruword2tags
RUN pip install .

RUN pip install git+https://github.com/Koziev/rutokenizer
RUN pip install git+https://github.com/Koziev/rupostagger
#RUN pip install git+https://github.com/Koziev/ruword2tags
RUN pip install git+https://github.com/Koziev/rusyllab
RUN pip install git+https://github.com/Koziev/ruchunker

RUN apt-get clean

WORKDIR /chatbot/ruchatbot/bot
COPY ./ruchatbot/bot/*.py ./

WORKDIR /chatbot/ruchatbot/frontend
COPY ./ruchatbot/frontend/*.py ./

WORKDIR /chatbot/ruchatbot/bot_service
COPY ./ruchatbot/bot_service/*.py ./

WORKDIR /chatbot/ruchatbot/bot_service/static/img
COPY ./ruchatbot/bot_service/static/img/*.* ./

WORKDIR /chatbot/ruchatbot/bot_service/templates
COPY ./ruchatbot/bot_service/templates/*.* ./


WORKDIR /chatbot/ruchatbot/utils
COPY ./ruchatbot/utils/*.py ./

WORKDIR /chatbot/ruchatbot/layers
COPY ./ruchatbot/layers/*.py ./

WORKDIR /chatbot/ruchatbot/scenarios
COPY ./ruchatbot/scenarios/*.py ./

WORKDIR /chatbot/ruchatbot/generative_grammar
COPY ./ruchatbot/generative_grammar/*.py ./

WORKDIR /chatbot/ruchatbot
COPY ./ruchatbot/__init__.py ./
COPY ./ruchatbot/qa_machine.py ./

WORKDIR /chatbot/data
COPY ./data/*.* ./

WORKDIR /chatbot/tmp/rugpt_chitchat
COPY ./tmp/rugpt_chitchat/*.* ./

WORKDIR /chatbot/tmp/rugpt_premise4question
COPY ./tmp/rugpt_premise4question/*.* ./

WORKDIR /chatbot/tmp
COPY ./tmp/*.* ./

WORKDIR /chatbot/scripts
COPY ./scripts/console_bot.sh ./
COPY ./scripts/flask_bot.sh ./
COPY ./scripts/tg_bot.sh ./

WORKDIR /chatbot
COPY CHANGELOG.txt ./

EXPOSE 9001
WORKDIR /chatbot/scripts
CMD "./console_bot.sh"
