"""Thread-safe, per-team sessions and duplicate-round idempotence."""
from threading import RLock
from copy import deepcopy
from .memory import Memory
from .brain import decide_response


class Agent:
    def __init__(self):
        self.sessions = {}
        self.lock = RLock()

    def respond(self,payload):
        team = payload['teamOur']
        key=(str(team.get('teamId','')),str(team.get('type','')))
        round_no=int(payload['roundNo'])
        with self.lock:
            memory=self.sessions.get(key)
            if memory is None or round_no<memory.round_no:
                memory=Memory()
                self.sessions[key]=memory
            if memory.round_no==round_no and memory.response is not None:
                return deepcopy(memory.response)
            # Commit memory only after a complete decision. A malformed request
            # must not cache the previous round's response under a new round.
            working=deepcopy(memory)
            response=decide_response(payload,working)
            self.sessions[key]=working
            return response
