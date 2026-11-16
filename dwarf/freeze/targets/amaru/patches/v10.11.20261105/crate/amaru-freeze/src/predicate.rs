// Copyright 2026 PRAGMA
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

//! Conditions over items, evaluated inside the node so that running to a distant point is cheap.
//!
//! A predicate is a JSON tree. Combinators: `{"op":"and","args":[..]}`, `{"op":"or","args":[..]}`,
//! `{"op":"not","arg":..}`, `{"op":"true"}`. Item tests: `{"op":"kind","in":[..]}`,
//! `{"op":"cold_spot","eq":".."}`, `{"op":"seq","ge":n}`, and text tests on `to`, `from`, `type`
//! and `label` using one of `eq`, `prefix`, `suffix`, `contains`.
//! Payload tests: `{"op":"field","path":"a.b[0]","eq"|"ne"|"lt"|"gt"|"contains"|"len_gt": v}`;
//! `path` is resolved inside the item's `payload`, or inside the whole item if it starts with `/`.

use serde_json::Value as Json;

#[derive(Debug, Clone, PartialEq)]
pub enum Predicate {
    True,
    And(Vec<Predicate>),
    Or(Vec<Predicate>),
    Not(Box<Predicate>),
    Kind(Vec<String>),
    ColdSpot(String),
    SeqGe(u64),
    Text { attribute: &'static str, test: TextTest },
    Field { path: Vec<Step>, from_item: bool, test: FieldTest },
}

#[derive(Debug, Clone, PartialEq)]
pub enum TextTest {
    Eq(String),
    Prefix(String),
    Suffix(String),
    Contains(String),
}

#[derive(Debug, Clone, PartialEq)]
pub enum FieldTest {
    Exists,
    Eq(Json),
    Ne(Json),
    Lt(f64),
    Gt(f64),
    Contains(String),
    LenGt(usize),
}

#[derive(Debug, Clone, PartialEq)]
pub enum Step {
    Key(String),
    Index(usize),
}

impl Predicate {
    pub fn parse(json: &Json) -> Result<Self, String> {
        let op = json.get("op").and_then(Json::as_str).ok_or("predicate needs a text `op`")?;
        let args = || -> Result<Vec<Predicate>, String> {
            json.get("args")
                .and_then(Json::as_array)
                .ok_or(format!("`{op}` needs `args`"))?
                .iter()
                .map(Predicate::parse)
                .collect()
        };
        Ok(match op {
            "true" => Predicate::True,
            "and" => Predicate::And(args()?),
            "or" => Predicate::Or(args()?),
            "not" => Predicate::Not(Box::new(Predicate::parse(json.get("arg").ok_or("`not` needs `arg`")?)?)),
            "kind" => Predicate::Kind(
                json.get("in")
                    .and_then(Json::as_array)
                    .ok_or("`kind` needs `in`")?
                    .iter()
                    .filter_map(|k| k.as_str().map(str::to_string))
                    .collect(),
            ),
            "cold_spot" => {
                Predicate::ColdSpot(json.get("eq").and_then(Json::as_str).ok_or("`cold_spot` needs `eq`")?.to_string())
            }
            "seq" => Predicate::SeqGe(json.get("ge").and_then(Json::as_u64).ok_or("`seq` needs `ge`")?),
            "to" => Predicate::Text { attribute: "to", test: TextTest::parse(json)? },
            "from" => Predicate::Text { attribute: "from", test: TextTest::parse(json)? },
            "type" => Predicate::Text { attribute: "type", test: TextTest::parse(json)? },
            "label" => Predicate::Text { attribute: "label", test: TextTest::parse(json)? },
            "field" => {
                let raw = json.get("path").and_then(Json::as_str).ok_or("`field` needs `path`")?;
                let (from_item, raw) = match raw.strip_prefix('/') {
                    Some(rest) => (true, rest),
                    None => (false, raw),
                };
                Predicate::Field { path: parse_path(raw)?, from_item, test: FieldTest::parse(json)? }
            }
            other => return Err(format!("unknown predicate op `{other}`")),
        })
    }

    pub fn matches(&self, item: &Json) -> bool {
        match self {
            Predicate::True => true,
            Predicate::And(all) => all.iter().all(|p| p.matches(item)),
            Predicate::Or(any) => any.iter().any(|p| p.matches(item)),
            Predicate::Not(inner) => !inner.matches(item),
            Predicate::Kind(kinds) => {
                item.get("kind").and_then(Json::as_str).is_some_and(|k| kinds.iter().any(|x| x == k))
            }
            Predicate::ColdSpot(tag) => item
                .get("cold_spots")
                .and_then(Json::as_array)
                .is_some_and(|spots| spots.iter().any(|s| s.as_str() == Some(tag))),
            Predicate::SeqGe(min) => item.get("seq").and_then(Json::as_u64).is_some_and(|s| s >= *min),
            Predicate::Text { attribute, test } => {
                item.get(*attribute).and_then(Json::as_str).is_some_and(|text| test.matches(text))
            }
            Predicate::Field { path, from_item, test } => {
                let root = if *from_item { Some(item) } else { item.get("payload") };
                test.matches(root.and_then(|root| resolve(root, path)))
            }
        }
    }
}

impl TextTest {
    fn parse(json: &Json) -> Result<Self, String> {
        let text = |key: &str| json.get(key).and_then(Json::as_str).map(str::to_string);
        if let Some(v) = text("eq") {
            Ok(TextTest::Eq(v))
        } else if let Some(v) = text("prefix") {
            Ok(TextTest::Prefix(v))
        } else if let Some(v) = text("suffix") {
            Ok(TextTest::Suffix(v))
        } else if let Some(v) = text("contains") {
            Ok(TextTest::Contains(v))
        } else {
            Err("text test needs one of `eq`, `prefix`, `suffix`, `contains`".to_string())
        }
    }

    fn matches(&self, text: &str) -> bool {
        match self {
            TextTest::Eq(v) => text == v,
            TextTest::Prefix(v) => text.starts_with(v.as_str()),
            TextTest::Suffix(v) => text.ends_with(v.as_str()),
            TextTest::Contains(v) => text.contains(v.as_str()),
        }
    }
}

impl FieldTest {
    fn parse(json: &Json) -> Result<Self, String> {
        if let Some(v) = json.get("eq") {
            Ok(FieldTest::Eq(v.clone()))
        } else if let Some(v) = json.get("ne") {
            Ok(FieldTest::Ne(v.clone()))
        } else if let Some(v) = json.get("lt").and_then(number) {
            Ok(FieldTest::Lt(v))
        } else if let Some(v) = json.get("gt").and_then(number) {
            Ok(FieldTest::Gt(v))
        } else if let Some(v) = json.get("contains").and_then(Json::as_str) {
            Ok(FieldTest::Contains(v.to_string()))
        } else if let Some(v) = json.get("len_gt").and_then(Json::as_u64) {
            Ok(FieldTest::LenGt(v as usize))
        } else if json.get("exists").and_then(Json::as_bool) == Some(true) {
            Ok(FieldTest::Exists)
        } else {
            Err("field test needs one of `eq`, `ne`, `lt`, `gt`, `contains`, `len_gt`, `exists`".to_string())
        }
    }

    fn matches(&self, value: Option<&Json>) -> bool {
        let Some(value) = value else {
            return matches!(self, FieldTest::Ne(_));
        };
        match self {
            FieldTest::Exists => true,
            FieldTest::Eq(expected) => value == expected,
            FieldTest::Ne(expected) => value != expected,
            FieldTest::Lt(limit) => number(value).is_some_and(|n| n < *limit),
            FieldTest::Gt(limit) => number(value).is_some_and(|n| n > *limit),
            FieldTest::Contains(needle) => match value {
                Json::String(s) => s.contains(needle.as_str()),
                Json::Object(o) => match o.get("$bytes") {
                    Some(Json::String(hex_text)) => hex_text.contains(needle.as_str()),
                    _ => o.contains_key(needle.as_str()),
                },
                Json::Array(items) => items.iter().any(|i| i.as_str() == Some(needle.as_str())),
                Json::Null | Json::Bool(_) | Json::Number(_) => false,
            },
            FieldTest::LenGt(limit) => length(value).is_some_and(|len| len > *limit),
        }
    }
}

/// Numbers, including large integers carried as `{"$int": "..."}`.
fn number(value: &Json) -> Option<f64> {
    match value {
        Json::Number(n) => n.as_f64(),
        Json::Object(o) => o.get("$int").and_then(Json::as_str).and_then(|s| s.parse::<f64>().ok()),
        Json::Null | Json::Bool(_) | Json::String(_) | Json::Array(_) => None,
    }
}

fn length(value: &Json) -> Option<usize> {
    match value {
        Json::String(s) => Some(s.len()),
        Json::Array(items) => Some(items.len()),
        Json::Object(o) => match (o.get("$bytes"), o.get("$map")) {
            (Some(Json::String(hex_text)), _) => Some(hex_text.len() / 2),
            (_, Some(Json::Array(pairs))) => Some(pairs.len()),
            _ => Some(o.len()),
        },
        Json::Null | Json::Bool(_) | Json::Number(_) => None,
    }
}

fn parse_path(raw: &str) -> Result<Vec<Step>, String> {
    let mut steps = Vec::new();
    for part in raw.split('.').filter(|p| !p.is_empty()) {
        let (key, mut rest) = match part.find('[') {
            Some(at) => (&part[..at], &part[at..]),
            None => (part, ""),
        };
        if !key.is_empty() {
            steps.push(Step::Key(key.to_string()));
        }
        while let Some(stripped) = rest.strip_prefix('[') {
            let end = stripped.find(']').ok_or(format!("unclosed `[` in path `{raw}`"))?;
            let index = stripped[..end].parse::<usize>().map_err(|_| format!("bad index in path `{raw}`"))?;
            steps.push(Step::Index(index));
            rest = &stripped[end + 1..];
        }
    }
    Ok(steps)
}

fn resolve<'a>(root: &'a Json, path: &[Step]) -> Option<&'a Json> {
    let mut current = root;
    for step in path {
        current = match step {
            Step::Key(key) => current.get(key.as_str())?,
            Step::Index(index) => current.get(*index)?,
        };
    }
    Some(current)
}

#[cfg(test)]
mod tests {
    use serde_json::json;

    use super::*;

    fn item() -> Json {
        json!({
            "seq": 48212, "kind": "proto_in", "to": "stage:validate_block", "from": "peer:10.0.0.12:3001",
            "type": "amaru_protocols::blockfetch::messages::Message", "label": "Block",
            "cold_spots": ["block-check", "wire-in"],
            "payload": { "slot": 91440112, "txs": [{ "fee": 301204 }, { "fee": { "$int": "99999999999999999999" } }],
                         "body": { "$bytes": "00ff10" } }
        })
    }

    fn check(predicate: Json) -> bool {
        Predicate::parse(&predicate).unwrap().matches(&item())
    }

    #[test]
    fn item_attributes() {
        assert!(check(json!({ "op": "kind", "in": ["proto_in", "wire_in"] })));
        assert!(!check(json!({ "op": "kind", "in": ["timer"] })));
        assert!(check(json!({ "op": "cold_spot", "eq": "block-check" })));
        assert!(check(json!({ "op": "to", "suffix": "validate_block" })));
        assert!(check(json!({ "op": "from", "contains": "10.0.0.12" })));
        assert!(check(json!({ "op": "seq", "ge": 48212 })));
        assert!(!check(json!({ "op": "seq", "ge": 48213 })));
    }

    #[test]
    fn payload_fields() {
        assert!(check(json!({ "op": "field", "path": "slot", "gt": 91440100 })));
        assert!(check(json!({ "op": "field", "path": "txs[0].fee", "eq": 301204 })));
        assert!(check(json!({ "op": "field", "path": "txs[1].fee", "gt": 1e18 })));
        assert!(check(json!({ "op": "field", "path": "txs", "len_gt": 1 })));
        assert!(check(json!({ "op": "field", "path": "body", "len_gt": 2 })));
        assert!(check(json!({ "op": "field", "path": "body", "contains": "ff10" })));
        assert!(check(json!({ "op": "field", "path": "/label", "eq": "Block" })));
        assert!(!check(json!({ "op": "field", "path": "missing", "eq": 1 })));
        assert!(check(json!({ "op": "field", "path": "missing", "ne": 1 })));
    }

    #[test]
    fn combinators() {
        assert!(check(json!({ "op": "and", "args": [
            { "op": "cold_spot", "eq": "block-check" },
            { "op": "not", "arg": { "op": "field", "path": "slot", "lt": 91440100 } }
        ]})));
        assert!(check(json!({ "op": "or", "args": [{ "op": "kind", "in": ["timer"] }, { "op": "true" }] })));
    }

    #[test]
    fn bad_predicates_are_reported() {
        assert!(Predicate::parse(&json!({ "op": "nope" })).is_err());
        assert!(Predicate::parse(&json!({ "op": "field", "path": "a[x]", "eq": 1 })).is_err());
        assert!(Predicate::parse(&json!({ "op": "to" })).is_err());
    }
}
