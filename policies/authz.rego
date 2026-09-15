package authz

import rego.v1

# allow is true when the caller is registered and the intent is in their permitted list.
# Input: {"caller_id": "<id>", "intent": "<intent>"}
allow if {
    caller := data.callers[input.caller_id]
    input.intent in caller.allowed_intents
}

# allowed_intents returns the list of permitted intents for a given caller.
# Defaults to an empty array when the caller is not registered.
# Input: {"caller_id": "<id>"}
default allowed_intents := []

allowed_intents := data.callers[input.caller_id].allowed_intents
