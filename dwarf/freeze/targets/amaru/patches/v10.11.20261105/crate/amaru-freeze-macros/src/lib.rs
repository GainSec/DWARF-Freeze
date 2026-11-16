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
//! The attribute behind `amaru_freeze::trace_fn`.

use proc_macro::TokenStream;
use proc_macro2::{Delimiter, Group, TokenStream as TokenStream2, TokenTree};
use quote::quote;
use syn::{
    FnArg, ItemFn, LitStr, Pat, ReturnType, Token,
    parse::{Parse, ParseStream},
};

/// What may be written between the parentheses of the attribute: nothing, or `in = "Type"`.
struct Options {
    owner: Option<String>,
}

impl Parse for Options {
    fn parse(input: ParseStream<'_>) -> syn::Result<Self> {
        if input.is_empty() {
            return Ok(Self { owner: None });
        }
        input.parse::<Token![in]>()?;
        input.parse::<Token![=]>()?;
        let owner = input.parse::<LitStr>()?.value();
        if !input.is_empty() {
            return Err(input.error("expected nothing after `in = \"Type\"`"));
        }
        Ok(Self { owner: Some(owner).filter(|owner| !owner.is_empty()) })
    }
}

/// Report every call of this function to the freeze agent while a debugger has it enabled.
///
/// The function starts by taking a guard from `amaru_freeze::calls::enter`, which costs one
/// atomic load while the function is not enabled. `#[trace(in = "Type")]` names the type or
/// trait whose `impl` holds the function, which the attribute cannot see for itself; the
/// function is then known as `crate::module::Type::function`.
///
/// Arguments bound to a plain name are reported with their `Debug` text when their type has
/// one. `self` is not reported. A function without a body and a `const fn` are left as they are.
///
/// An `async fn` first awaits `amaru_freeze::calls::held`, which is where its call waits when a
/// debugger holds it. Its body then runs inside `amaru_freeze::calls::framed`, so that what it
/// calls reports it as the caller, across awaits too. An `async fn` whose return type contains
/// `impl Trait` keeps its body as written, because the type cannot be named for the block the
/// body would move into; its callees do not see it as their caller.
#[proc_macro_attribute]
pub fn trace(options: TokenStream, item: TokenStream) -> TokenStream {
    let options = syn::parse_macro_input!(options as Options);
    match traced(&options, item.clone().into()) {
        Some(traced) => traced.into(),
        None => item,
    }
}

/// The function with the guard at the top of its body, or `None` for an item to leave alone.
fn traced(options: &Options, item: TokenStream2) -> Option<TokenStream2> {
    let mut tokens: Vec<TokenTree> = item.clone().into_iter().collect();
    let Some(TokenTree::Group(body)) =
        tokens.pop().filter(|last| matches!(last, TokenTree::Group(group) if group.delimiter() == Delimiter::Brace))
    else {
        return None;
    };
    let mut start = body.stream().into_iter();
    if let (Some(TokenTree::Punct(hash)), Some(TokenTree::Punct(bang))) = (start.next(), start.next())
        && hash.as_char() == '#'
        && bang.as_char() == '!'
    {
        return None;
    }

    let signature = syn::parse2::<ItemFn>(item).ok()?.sig;
    if signature.constness.is_some() {
        return None;
    }

    let name = signature.ident.to_string();
    let name = name.strip_prefix("r#").unwrap_or(&name);
    let path = match &options.owner {
        Some(owner) => format!("::{owner}::{name}"),
        None => format!("::{name}"),
    };
    let is_async = signature.asyncness.is_some();

    let arguments = signature.inputs.iter().filter_map(|argument| {
        let FnArg::Typed(argument) = argument else {
            return None;
        };
        let Pat::Ident(pattern) = &*argument.pat else {
            return None;
        };
        if pattern.subpat.is_some() || pattern.ident == "self" {
            return None;
        }
        let ident = &pattern.ident;
        let shown = ident.to_string();
        let shown = shown.strip_prefix("r#").unwrap_or(&shown);
        let conditions = argument.attrs.iter().filter(|attribute| attribute.path().is_ident("cfg"));
        Some(quote! {
            #(#conditions)*
            #[allow(clippy::needless_borrow)]
            {
                __freeze_args.push(#shown, (&::amaru_freeze::calls::Shown(&#ident)).freeze_text());
            }
        })
    });

    let statements = body.stream();
    let enter = quote! {
        static __FREEZE_SITE: ::amaru_freeze::calls::Site =
            ::amaru_freeze::calls::Site::new(concat!(module_path!(), #path), #name, #is_async);
        #[allow(unused_mut)]
        let mut __freeze_guard = ::amaru_freeze::calls::enter(&__FREEZE_SITE, || {
            #[allow(unused_imports)]
            use ::amaru_freeze::calls::{ShowDebug as _, ShowOpaque as _};
            #[allow(unused_mut)]
            let mut __freeze_args = ::amaru_freeze::calls::Args::new();
            #(#arguments)*
            __freeze_args
        });
    };
    let returned = match &signature.output {
        ReturnType::Default => quote! { () },
        ReturnType::Type(_, returned) => quote! { #returned },
    };
    let traced_statements = if !is_async {
        quote! {
            #enter
            #statements
        }
    } else if mentions_impl(returned.clone()) {
        quote! {
            #enter
            ::amaru_freeze::calls::held(&mut __freeze_guard).await;
            #statements
        }
    } else {
        quote! {
            #enter
            ::amaru_freeze::calls::held(&mut __freeze_guard).await;
            let __freeze_body = ::core::pin::pin!(async move {
                #[allow(
                    unknown_lints,
                    unreachable_code,
                    clippy::diverging_sub_expression,
                    clippy::empty_loop,
                    clippy::let_unit_value,
                    clippy::let_with_type_underscore,
                    clippy::needless_return,
                    clippy::unreachable
                )]
                if false {
                    let __freeze_returned: #returned = loop {};
                    return __freeze_returned;
                }
                #statements
            });
            ::amaru_freeze::calls::framed(__freeze_guard.frame(), __freeze_body).await
        }
    };
    let mut traced_body = Group::new(Delimiter::Brace, traced_statements);
    traced_body.set_span(body.span());
    tokens.push(TokenTree::Group(traced_body));
    Some(tokens.into_iter().collect())
}

/// Whether `impl` occurs anywhere in a type, which then cannot be written in a `let`.
fn mentions_impl(tokens: TokenStream2) -> bool {
    tokens.into_iter().any(|token| match token {
        TokenTree::Ident(ident) => ident == "impl",
        TokenTree::Group(group) => mentions_impl(group.stream()),
        TokenTree::Punct(_) | TokenTree::Literal(_) => false,
    })
}
