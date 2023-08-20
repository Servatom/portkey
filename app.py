import datetime
from flask import Flask, request, jsonify
import openai
from dotenv import load_dotenv
import os
import re
import requests
import logging
import redis

app = Flask(__name__)
redisHost = os.environ.get("REDIS_HOST")
redisClient = redis.Redis(host=redisHost, port=6379, db=0)

LOGGER = logging.getLogger(__name__)
load_dotenv()

openai.api_key = os.getenv('OPENAI_API_KEY')
scraper_api = os.getenv('SCRAPER_BASE_URL')
DIAGON_ALLEY_BASE_URL = os.environ.get("DA_BASE_URL")

class Product:
    def __init__(self, name, price, color):
        self.name = name
        self.price = price
        self.color = color
    @staticmethod
    def multi_product_to_string(products):
        result = ""
        # maintain index and print like:
        # Product 1: details, Product 2: details
        for index, product in enumerate(products):
            result += "Product {}: {}\n".format(index + 1, product)
        return result
    
    def __repr__(self):
        return "Name: {}, Price: {}, Color: {}".format(self.name, self.price, self.color)

class Route:
    def __init__(self, route, method):
        self.route = route
        self.method = method
    def get_details(self):
        return {
            "url": DIAGON_ALLEY_BASE_URL + self.route,
            "method": self.method
        }

class DiagonAlleyClient:
    ORDER_HISTORY = Route("/order/all", "GET")
    USER_PROFILE = Route("/auth/user/me", "GET")

    def __init__(self, bearer_token):
        self.bearer_token = bearer_token
    
    def _request_creator(self, route: Route, body=None):
        details = route.get_details()
        if details["method"] == "GET":
            response = requests.get(details["url"], headers={"Authorization": self.bearer_token})
            return response
        elif details["method"] == "POST":
            response = requests.post(details["url"], headers={"Authorization": self.bearer_token}, json=body)
            return response
        else:
            raise Exception("Method not supported")

    def _get_order_history(self):
        response = self._request_creator(self.ORDER_HISTORY)
        if response.status_code != 200:
            LOGGER.error("Error in getting order history")
        return response.json()
    
    def user_product_history(self):
        order_history = self._get_order_history()
        products_bought = []
        for order in order_history:
            for product in order["products"]:
                products_bought.append(Product(product["name"], product["price"], product["color"]))
        return products_bought
    
    def get_user_persona(self):
        response = self._request_creator(self.USER_PROFILE)
        if response.status_code != 200:
            LOGGER.error("Error in getting user profile")
        gender = response.json()["gender"]
        age = response.json()["age"]
        name = response.json()["name"]
        return f"{name} who is a {gender} of age {age}"

@app.route("/init", methods=['GET'])
def init_conversation():
    # check headers for bearer token
    bearer_token = request.headers.get('Authorization')
    if not bearer_token:
        return jsonify({"error": "No bearer token found"})
    diagon_alley = DiagonAlleyClient(bearer_token)
    products_bought = diagon_alley.user_product_history()

    conversation_init = [
        {"role": "system", "content": "You are an outfit recommender. You converse with the user, take in their suggestions and choices, ask for details, take their previous order history into account, and generate small search strings for them to search fashion websites"},
        {"role": "system", "content": "Suggest clothes for {}".format(diagon_alley.get_user_persona())}
    ]

    if len(products_bought) > 0:
        conversation_init.append({"role": "system", "content": "You are going to be provided with the user's previously ordered products. This will help you to understand them more"})
        conversation_init.append({"role": "system", "content": "The user has bought the following products in the past: {}".format(Product.multi_product_to_string(products_bought))})
        conversation_init.append({"role": "system", "content": "You can use the name, color and price to estimate the kind of user preference. You can still ask these questions to the user, but this might influence your search string"})
    
    remainder_conversation = [
        {"role": "system", "content": "You have to ask users questions to get their preferences around colour, their budget, occasion"},
        {"role": "system", "content": "Get these details from users unless they tell you that they don't have a preference and then generate a search string"},
        {"role": "system", "content": "The gender provided earlier is very important. Include it in the search string as well"}
    ]

    conversation_init.extend(remainder_conversation)

    print(conversation_init)
    # generate a unique code
    redis_key = int(datetime.datetime.now().timestamp())
    # store json in redis
    conversation_to_bytes = str(conversation_init).encode('utf-8')
    redisClient.set(redis_key, conversation_to_bytes, ex=86400)

    return jsonify({"conversation_id": redis_key})
    
@app.route('/talk/<conversationID>', methods=['POST'])
def get_bot_response(conversationID):
    if not conversationID:
        return jsonify({"error": "No conversation ID found"})
    # get conversation from redis
    conversation = redisClient.get(conversationID)
    if not conversation:
        return jsonify({"error": "Conversation not found"})
    conversation = conversation.decode('utf-8')
    conversation = eval(conversation)
    
    try:
        data = request.get_json()

        # Extract the user input from the conversation data
        user_input = data['conversation']

        for msg in user_input:
            conversation.append(msg)
        conversation.append(
            {"role": "system", "content": "One last thing. If you have got to know the user well, and you have a search_string which I can use to search for products. Format it like this: search_string = \"<search_string>\""}
        )
        conversation.append({"role": "system", "content": "Format when giving search string is: search_string='search_string'"})
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=conversation,
            max_tokens=100,
        )

        LOGGER.info("Response created")
        bot_reply = response['choices'][0]['message']['content']
        # extract search_string from bot_reply
        json_match = re.search(r"search_string=(.*)", bot_reply) or re.search(r"search_string = (.*)", bot_reply)
        if json_match:
            LOGGER.info("Final search term to be returned")
            search_string = json_match.group(1)
            search_url = scraper_api + search_string.replace(" ", "%20").replace('"', "").replace("'", "")
            response = requests.get(search_url)
            
            # Return the search results from the API
            if response.status_code != 200:
                LOGGER.error("Error in getting search results")
            search_results = response.json()
            results_to_return = []
            if search_results.get("result"):
                results_to_return = search_results["result"][:5]
            return jsonify({"bot_reply_type": "search_results", "search_results": results_to_return})

        LOGGER.info("Continue conversation")
        # delete the last message from the conversation
        del conversation[-1]
        conversation.append({"role": "system", "content": bot_reply})
        print(conversation)
        redisClient.set(conversationID, str(conversation).encode('utf-8'), ex=86400)
        return jsonify({"bot_reply_type": "text", "bot_reply": bot_reply})
    
    except Exception as e:
        return jsonify({"error": str(e)})

if __name__ == '__main__':
    app.run(
        debug=True,
        host="0.0.0.0",
        port="6000"
    )