# Gmail trash batches under one confirm; other deletes stay single

Calendar and Tasks still delete one id per confirm. Gmail trash takes `message_ids` (max 25) and one `confirm=true` covers the set, because inbox triage is "search then act on the result list." That is a breaking change: `message_id` is gone. Partial failures are returned per id; they are not rolled back.
