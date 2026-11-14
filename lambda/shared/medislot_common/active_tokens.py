"""The active-tokens list: every access token that may still be used.

DynamoDB table medislot-<env>-active-tokens, key jti. Login and refresh add
the new access token; logout, refresh and account removal take tokens off.
The Lambda authorizer accepts a token only if its jti is on the list, so a
token that is not there (logged out, replaced by a refresh, account removed,
or simply unknown) is refused, even while its signature and expiry are fine.

Item: jti, userId, role, createdAt, expiresAt (the token's own exp; the
table's TTL removes the row once the token has expired anyway).
Index GSI_User_Tokens (userId) lists one user's tokens, so removing an account
signs it out on every device at once.
"""

from boto3.dynamodb.conditions import Key

from . import config

TABLE_VARIABLE = "ACTIVE_TOKENS_TABLE"
USER_INDEX = "GSI_User_Tokens"


def table():
    return config.table(TABLE_VARIABLE)


def item_for(claims, now):
    """The row that makes a freshly issued access token usable."""
    return {
        "jti": claims["jti"],
        "userId": claims["sub"],
        "role": claims["role"],
        "createdAt": int(now),
        "expiresAt": int(claims["exp"]),
    }


def is_active(claims):
    """True when the token's jti is on the list and belongs to the token's user.

    Strongly consistent read: a token issued a moment ago must already count.
    """
    found = table().get_item(Key={"jti": claims["jti"]}, ConsistentRead=True).get("Item")
    return bool(found) and found.get("userId") == claims["sub"]


def remove(jti):
    table().delete_item(Key={"jti": jti})


def remove_all_for_user(user_id):
    """Take every active token of one user off the list; returns how many."""
    tbl = table()
    removed = 0
    request = {"IndexName": USER_INDEX, "KeyConditionExpression": Key("userId").eq(user_id)}
    while True:
        page = tbl.query(**request)
        for row in page.get("Items", []):
            tbl.delete_item(Key={"jti": row["jti"]})
            removed += 1
        if "LastEvaluatedKey" not in page:
            return removed
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]
