from urllib.parse import parse_qsl, urlparse, unquote
from gevent.event import Event
from . import HTTPJob
from .utilities import JSONDecodeError, load_json, dump_json, static_routes, \
	generate_methods
from .database import Message, User, get_messages_by_timestamp, \
	get_user_by_name, set_message
from typing import Any, Dict, Callable, Union, List, Optional
from datetime import datetime as DateTime
from base64 import b64decode
from re import compile as regex_compile
from os import path

auth_regex = regex_compile(r"^(?:(\w+) )?(.*)$")
token_regex = regex_compile(r"^(\w+):(.*)$")

mut_message_event = Event()

class AnnotatedEndpoint():
	@staticmethod
	def annotate(expression: List[Optional[str]]):
		def decorator(endpoint: Callable[[HTTPJob, List[str]], None]):
			return AnnotatedEndpoint(expression, endpoint)
		return decorator

	def __init__(self, expression: List[Optional[str]],
			endpoint: Callable[[HTTPJob, List[str]], None]):
		self.expression = expression
		self._annotated_item = endpoint

	def __call__(self, job: HTTPJob):
		unmarked_paths = [
			job.path[ind] for ind, item in enumerate(self.expression) \
				if item is None
		]

		self._annotated_item(job, unmarked_paths)

def respond_error(job: HTTPJob, message: str, code: str = "400 Bad Request"):
	content = f'{{"message":"{message}"}}'.encode("utf-8")
	headers = {
		"Content-Type": "application/json; charset=utf-8",
		"Content-Length": str(len(content))
	}
	
	job.write_head(code, headers)
	job.close_body(content)

def get_authorized_user(job: HTTPJob):
	"""Gets the authorized user via the request's authorization header. If for any
	reason the authorization fails, this method automatically sends a response and
	returns nothing, allowing for a very easy implementation as shown below.

	```
	if (auth_user := get_authorized_user(job)) is None:
		return
	```
	"""

	auth: Optional[str] = job.headers.get("AUTHORIZATION")
	if auth is None:
		return respond_error(job, "Unauthorized.", "401 Unauthorized")

	auth_match = auth_regex.match(auth)
	if auth_match is None:
		return respond_error(job, "Invalid authorization header.")

	auth_type = auth_match[1]
	encoded_token = auth_match[2]
	if auth_type != "" and auth_type.lower() != "basic":
		return respond_error(job, "Unknown authorization type.")

	token = b64decode(encoded_token).decode("utf-8")
	token_match = token_regex.match(token)
	if token_match is None:
		return respond_error(job, "Invalid authorization token.")

	user_name = token_match[1]
	password = token_match[2]
	user = get_user_by_name(user_name)
	if user is None or user.password != password:
		return respond_error(job, "Bad authorization token.")

	return user

def on_get_messages_request(job: HTTPJob, authed_user: User):
	query = {key: val for key, val in job.query}
	before_raw = query.get("before")
	after_raw = query.get("after")
	polling_raw = query.get("polling")
	limit_raw = query.get("limit")

	before = None if before_raw is None else \
		float(before_raw) if before_raw.isnumeric() else -1
	after = None if after_raw is None else \
		float(after_raw) if after_raw.isnumeric() else -1
	polling = polling_raw.lower() == "true" or polling_raw == "1" \
		if polling_raw is not None else True
	limit = None if limit_raw is None else \
		int(limit_raw) if limit_raw.isnumeric() else -1

	if before == -1:
		return respond_error(job, "Invalid query paramater for before.")
	if after == -1:
		return respond_error(job, "Invalid query paramater for after.")
	if limit == -1:
		return respond_error(job, "Invalid query paramater for limit.")
	if before is not None and after is not None:
		return respond_error(job,
			"Query paramaters before and after are mutually exclusive.")
	if limit is not None and (0 >= limit or limit > 200):
		return respond_error(job, "Query paramater limit was out of range. " +
			"Must be between 1 and 200 inclusive.")

	timestamp = before if before is not None else after \
		if after is not None else DateTime.now().timestamp()
	is_before = True if before is not None or after is None else False
	messages = get_messages_by_timestamp(timestamp, is_before, limit if \
		limit is not None else 50)

	print(len(messages))
	print(polling)
	print(is_before)

	if len(messages) == 0 and polling and not is_before:
		mut_message_event.wait(60)
		messages = get_messages_by_timestamp(timestamp, False, 1)

		users = [
			user for user in \
				{get_user_by_name(message.author) for message in messages} \
					if user is not None
		]

		content = dump_json({"users": users, "messages": messages}, indent=None)
		job.write_head("200 OK", {
			"Content-Type": "application/json; charset=utf-8",
			"Content-Length": str(len(content))
		})
		job.close_body(content)
	else:
		users = [
			user for user in \
				{get_user_by_name(message.author) for message in messages} \
					if user is not None
		]

		content = dump_json({"users": users, "messages": messages}, indent=None)
		job.write_head("200 OK", {
			"Content-Type": "application/json; charset=utf-8",
			"Content-Length": str(len(content))
		})
		job.close_body(content)

def on_post_messages_request(job: HTTPJob, authed_user: User):
	global mut_message_event

	body = job.body.read()
	json_body: Dict[str, Any]
	try:
		json_body = load_json(body)
	except JSONDecodeError:
		return respond_error(job, "Invalid body.")
	else:
		if type(json_body) is not dict:
			return respond_error(job, "Bad json structure.")

	content_unstripped: str = json_body.get("content") # type: ignore
	if type(content_unstripped) is not str: # THIS DOESN'T REDUCE THE TYPE! Q~Q
		return respond_error(job, "Bad json structure.")

	content = content_unstripped.strip()
	if content == "":
		return respond_error(job, "Cannot send empty message.")

	message = Message(DateTime.now().timestamp(), authed_user, content)
	set_message(message)

	mut_message_event.set()
	mut_message_event = Event()
	message_json = dump_json(message, indent=None)
	job.write_head("200 OK", {
		"Content-Type": "application/json; charset=utf-8",
		"Content-Length": str(len(message_json))
	})
	job.close_body(message_json)

@AnnotatedEndpoint.annotate(["api", "v1", "communities", None, "channels", None, "messages"])
@generate_methods(methods=["HEAD", "OPTIONS", "GET", "POST"])
def on_messages_request(job: HTTPJob, path: List[str]):
	if (authed_user := get_authorized_user(job)) is None:
		return

	if path[0] != "_" or path[1] != "_":
		job.write_head("404 Not Found", {})
		job.close_body()

	methods = {
		"GET": on_get_messages_request,
		"POST": on_post_messages_request
	}

	methods.get(job.method)(job, authed_user)

internal_endpoints = [
	on_messages_request
]

def on_request(job: HTTPJob):
	# api.site.com/v1/communities/{community_name_id}/channels/{channel_name_id}/messages
	# api.site.com/v1/communities/_/channels/_/messages
	# https://website-schooll.herokuapp.com/api/v1/communities/_/channels/_/messages?max=50&polling=true

	url = urlparse(job.uri)
	paths = [unquote(path) for path in url.path.split("/")][1:]
	query = parse_qsl(url.query)

	endpoint = next((endpoint for endpoint in internal_endpoints \
		if len(endpoint.expression) == len(paths) and \
			all((end_part is None or end_part == paths[ind]) \
				for ind, end_part in enumerate(endpoint.expression))), None)
	
	if endpoint is not None:
		endpoint(job)
	else:
		job.write_head("404 Not Found", {})
		job.close_body()

front_end_points = {
	key: val \
		for endpoint, fil in (load_json(open(path.join(path.dirname(__file__),
			"../frontendmap.json"), "r").read()).items())
				for key, val in static_routes([endpoint],
					file=path.join(path.dirname(__file__), "../assets", fil)).items()
}

back_end_points = {
	None: on_request
}

endpoints: Dict[Union[str, None], Callable[[HTTPJob], None]] = \
	dict(back_end_points, **front_end_points)
