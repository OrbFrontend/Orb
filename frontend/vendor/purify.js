/*! @license DOMPurify 3.4.15 | (c) Cure53 and other contributors | Released under the Apache license 2.0 and Mozilla Public License 2.0 | github.com/cure53/DOMPurify/blob/3.4.15/LICENSE */
function _arrayLikeToArray(r,a){(null==a||a>r.length)&&(a=r.length);for(var e=0,n=Array(a);e<a;e++)n[e]=r[e];return n}
function _arrayWithHoles(r){if(Array.isArray(r))return r}function _iterableToArrayLimit(r,l){
var t=null==r?null:"undefined"!=typeof Symbol&&r[Symbol.iterator]||r["@@iterator"];if(null!=t){
var e,n,i,u,a=[],f=true,o=false;try{if(i=(t=t.call(r)).next,0===l);else for(;!(f=(e=i.call(t)).done)&&(a.push(e.value),
a.length!==l);f=!0);}catch(r){o=true,n=r}finally{try{if(!f&&null!=t.return&&(u=t.return(),Object(u)!==u))return}finally{
if(o)throw n}}return a}}function _nonIterableRest(){
throw new TypeError("Invalid attempt to destructure non-iterable instance.\nIn order to be iterable, non-array objects must have a [Symbol.iterator]() method.")
}function _slicedToArray(r,e){
return _arrayWithHoles(r)||_iterableToArrayLimit(r,e)||_unsupportedIterableToArray(r,e)||_nonIterableRest()}
function _unsupportedIterableToArray(r,a){if(r){if("string"==typeof r)return _arrayLikeToArray(r,a)
;var t={}.toString.call(r).slice(8,-1);return"Object"===t&&r.constructor&&(t=r.constructor.name),
"Map"===t||"Set"===t?Array.from(r):"Arguments"===t||/^(?:Ui|I)nt(?:8|16|32)(?:Clamped)?Array$/.test(t)?_arrayLikeToArray(r,a):void 0
}}
const entries=Object.entries,setPrototypeOf=Object.setPrototypeOf,isFrozen=Object.isFrozen,getPrototypeOf=Object.getPrototypeOf,getOwnPropertyDescriptor=Object.getOwnPropertyDescriptor
;let freeze=Object.freeze,seal=Object.seal,create=Object.create
;let _ref=typeof Reflect!=="undefined"&&Reflect,apply=_ref.apply,construct=_ref.construct;if(!freeze){
freeze=function freeze(x){return x}}if(!seal){seal=function seal(x){return x}}if(!apply){
apply=function apply(func,thisArg){
for(var _len=arguments.length,args=new Array(_len>2?_len-2:0),_key=2;_key<_len;_key++){args[_key-2]=arguments[_key]}
return func.apply(thisArg,args)}}if(!construct){construct=function construct(Func){
for(var _len2=arguments.length,args=new Array(_len2>1?_len2-1:0),_key2=1;_key2<_len2;_key2++){
args[_key2-1]=arguments[_key2]}return new Func(...args)}}const arrayForEach=unapply(Array.prototype.forEach)
;const arrayLastIndexOf=unapply(Array.prototype.lastIndexOf);const arrayPop=unapply(Array.prototype.pop)
;const arrayPush=unapply(Array.prototype.push);const arraySplice=unapply(Array.prototype.splice)
;const arrayIsArray=Array.isArray;const stringToLowerCase=unapply(String.prototype.toLowerCase)
;const stringToString=unapply(String.prototype.toString);const stringMatch=unapply(String.prototype.match)
;const stringReplace=unapply(String.prototype.replace);const stringIndexOf=unapply(String.prototype.indexOf)
;const stringTrim=unapply(String.prototype.trim);const numberToString=unapply(Number.prototype.toString)
;const booleanToString=unapply(Boolean.prototype.toString)
;const bigintToString=typeof BigInt==="undefined"?null:unapply(BigInt.prototype.toString)
;const symbolToString=typeof Symbol==="undefined"?null:unapply(Symbol.prototype.toString)
;const objectHasOwnProperty=unapply(Object.prototype.hasOwnProperty)
;const objectToString=unapply(Object.prototype.toString);const regExpTest=unapply(RegExp.prototype.test)
;const typeErrorCreate=unconstruct(TypeError);function unapply(func){return function(thisArg){
if(thisArg instanceof RegExp){thisArg.lastIndex=0}
for(var _len3=arguments.length,args=new Array(_len3>1?_len3-1:0),_key3=1;_key3<_len3;_key3++){
args[_key3-1]=arguments[_key3]}return apply(func,thisArg,args)}}function unconstruct(Func){return function(){
for(var _len4=arguments.length,args=new Array(_len4),_key4=0;_key4<_len4;_key4++){args[_key4]=arguments[_key4]}
return construct(Func,args)}}function addToSet(set,array){
let transformCaseFunc=arguments.length>2&&arguments[2]!==undefined?arguments[2]:stringToLowerCase;if(setPrototypeOf){
setPrototypeOf(set,null)}if(!arrayIsArray(array)){return set}let l=array.length;while(l--){let element=array[l]
;if(typeof element==="string"){const lcElement=transformCaseFunc(element);if(lcElement!==element){if(!isFrozen(array)){
array[l]=lcElement}element=lcElement}}set[element]=true}return set}function cleanArray(array){
for(let index=0;index<array.length;index++){const isPropertyExist=objectHasOwnProperty(array,index)
;if(!isPropertyExist){array[index]=null}}return array}function clone(object){const newObject=create(null)
;for(const _ref2 of entries(object)){var _ref3=_slicedToArray(_ref2,2);const property=_ref3[0];const value=_ref3[1]
;const isPropertyExist=objectHasOwnProperty(object,property);if(isPropertyExist){if(arrayIsArray(value)){
newObject[property]=cleanArray(value)}else if(value&&typeof value==="object"&&value.constructor===Object){
newObject[property]=clone(value)}else{newObject[property]=value}}}return newObject}function stringifyValue(value){
switch(typeof value){case"string":{return value}case"number":{return numberToString(value)}case"boolean":{
return booleanToString(value)}case"bigint":{return bigintToString?bigintToString(value):"0"}case"symbol":{
return symbolToString?symbolToString(value):"Symbol()"}case"undefined":{return objectToString(value)}case"function":
case"object":{if(value===null){return objectToString(value)}const valueAsRecord=value
;const valueToString=lookupGetter(valueAsRecord,"toString");if(typeof valueToString==="function"){
const stringified=valueToString(valueAsRecord)
;return typeof stringified==="string"?stringified:objectToString(stringified)}return objectToString(value)}default:{
return objectToString(value)}}}function lookupGetter(object,prop){while(object!==null){
const desc=getOwnPropertyDescriptor(object,prop);if(desc){if(desc.get){return unapply(desc.get)}
if(typeof desc.value==="function"){return unapply(desc.value)}}object=getPrototypeOf(object)}function fallbackValue(){
return null}return fallbackValue}function isRegex(value){try{regExpTest(value,"");return true}catch(_unused){
return false}}
const html$1=freeze(["a","abbr","acronym","address","area","article","aside","audio","b","bdi","bdo","big","blink","blockquote","body","br","button","canvas","caption","center","cite","code","col","colgroup","content","data","datalist","dd","decorator","del","details","dfn","dialog","dir","div","dl","dt","element","em","fieldset","figcaption","figure","font","footer","form","h1","h2","h3","h4","h5","h6","head","header","hgroup","hr","html","i","img","input","ins","kbd","label","legend","li","main","map","mark","marquee","menu","menuitem","meter","nav","nobr","ol","optgroup","option","output","p","picture","pre","progress","q","rp","rt","ruby","s","samp","search","section","select","shadow","slot","small","source","spacer","span","strike","strong","style","sub","summary","sup","table","tbody","td","template","textarea","tfoot","th","thead","time","tr","track","tt","u","ul","var","video","wbr"])

;const svg$1=freeze(["svg","a","altglyph","altglyphdef","altglyphitem","animatecolor","animatemotion","animatetransform","circle","clippath","defs","desc","ellipse","enterkeyhint","exportparts","filter","font","g","glyph","glyphref","hkern","image","inputmode","line","lineargradient","marker","mask","metadata","mpath","part","path","pattern","polygon","polyline","radialgradient","rect","stop","style","switch","symbol","text","textpath","title","tref","tspan","view","vkern"])

;const svgFilters=freeze(["feBlend","feColorMatrix","feComponentTransfer","feComposite","feConvolveMatrix","feDiffuseLighting","feDisplacementMap","feDistantLight","feDropShadow","feFlood","feFuncA","feFuncB","feFuncG","feFuncR","feGaussianBlur","feImage","feMerge","feMergeNode","feMorphology","feOffset","fePointLight","feSpecularLighting","feSpotLight","feTile","feTurbulence"])

;const svgDisallowed=freeze(["animate","color-profile","cursor","discard","font-face","font-face-format","font-face-name","font-face-src","font-face-uri","foreignobject","hatch","hatchpath","mesh","meshgradient","meshpatch","meshrow","missing-glyph","script","set","solidcolor","unknown","use"])

;const mathMl$1=freeze(["math","menclose","merror","mfenced","mfrac","mglyph","mi","mlabeledtr","mmultiscripts","mn","mo","mover","mpadded","mphantom","mroot","mrow","ms","mspace","msqrt","mstyle","msub","msup","msubsup","mtable","mtd","mtext","mtr","munder","munderover","mprescripts"])

;const mathMlDisallowed=freeze(["maction","maligngroup","malignmark","mlongdiv","mscarries","mscarry","msgroup","mstack","msline","msrow","semantics","annotation","annotation-xml","mprescripts","none"])
;const text=freeze(["#text"])
;const html=freeze(["accept","action","align","alt","autocapitalize","autocomplete","autopictureinpicture","autoplay","background","bgcolor","border","capture","cellpadding","cellspacing","checked","cite","class","clear","color","cols","colspan","command","commandfor","controls","controlslist","coords","crossorigin","datetime","decoding","default","dir","disabled","disablepictureinpicture","disableremoteplayback","download","draggable","enctype","enterkeyhint","exportparts","face","for","headers","height","hidden","high","href","hreflang","id","inert","inputmode","integrity","ismap","kind","label","lang","list","loading","loop","low","max","maxlength","media","method","min","minlength","multiple","muted","name","nonce","noshade","novalidate","nowrap","open","optimum","part","pattern","placeholder","playsinline","popover","popovertarget","popovertargetaction","poster","preload","pubdate","radiogroup","readonly","rel","required","rev","reversed","role","rows","rowspan","spellcheck","scope","selected","shape","size","sizes","slot","span","srclang","start","src","srcset","step","style","summary","tabindex","title","translate","type","usemap","valign","value","width","wrap","xmlns"])

;const svg=freeze(["accent-height","accumulate","additive","alignment-baseline","amplitude","ascent","attributename","attributetype","azimuth","basefrequency","baseline-shift","begin","bias","by","class","clip","clippathunits","clip-path","clip-rule","color","color-interpolation","color-interpolation-filters","color-profile","color-rendering","cx","cy","d","dx","dy","diffuseconstant","direction","display","divisor","dominant-baseline","dur","edgemode","elevation","end","exponent","fill","fill-opacity","fill-rule","filter","filterunits","flood-color","flood-opacity","font-family","font-size","font-size-adjust","font-stretch","font-style","font-variant","font-weight","fx","fy","g1","g2","glyph-name","glyphref","gradientunits","gradienttransform","height","href","id","image-rendering","in","in2","intercept","k","k1","k2","k3","k4","kerning","keypoints","keysplines","keytimes","lang","lengthadjust","letter-spacing","kernelmatrix","kernelunitlength","lighting-color","local","marker-end","marker-mid","marker-start","markerheight","markerunits","markerwidth","maskcontentunits","maskunits","max","mask","mask-type","media","method","mode","min","name","numoctaves","offset","operator","opacity","order","orient","orientation","origin","overflow","paint-order","path","pathlength","patterncontentunits","patterntransform","patternunits","pointer-events","points","preservealpha","preserveaspectratio","primitiveunits","r","rx","ry","radius","refx","refy","repeatcount","repeatdur","restart","result","rotate","scale","seed","shape-rendering","slope","specularconstant","specularexponent","spreadmethod","startoffset","stddeviation","stitchtiles","stop-color","stop-opacity","stroke-dasharray","stroke-dashoffset","stroke-linecap","stroke-linejoin","stroke-miterlimit","stroke-opacity","stroke","stroke-width","style","surfacescale","systemlanguage","tabindex","tablevalues","targetx","targety","transform","transform-origin","text-anchor","text-decoration","text-orientation","text-rendering","textlength","type","u1","u2","unicode","values","vector-effect","viewbox","visibility","version","vert-adv-y","vert-origin-x","vert-origin-y","width","word-spacing","wrap","writing-mode","xchannelselector","ychannelselector","x","x1","x2","xmlns","y","y1","y2","z","zoomandpan"])

;const mathMl=freeze(["accent","accentunder","align","bevelled","close","columnalign","columnlines","columnspacing","columnspan","denomalign","depth","dir","display","displaystyle","encoding","fence","frame","height","href","id","largeop","length","linethickness","lquote","lspace","mathbackground","mathcolor","mathsize","mathvariant","maxsize","minsize","movablelimits","notation","numalign","open","rowalign","rowlines","rowspacing","rowspan","rspace","rquote","scriptlevel","scriptminsize","scriptsizemultiplier","selection","separator","separators","stretchy","subscriptshift","supscriptshift","symmetric","voffset","width","xmlns"])
;const xml=freeze(["xlink:href","xml:id","xlink:title","xml:space","xmlns:xlink"])
;const MUSTACHE_EXPR=seal(/{{[\w\W]*|^[\w\W]*}}/g);const ERB_EXPR=seal(/<%[\w\W]*|^[\w\W]*%>/g)
;const TMPLIT_EXPR=seal(/\${[\w\W]*/g);const DATA_ATTR=seal(/^data-[\-\w.\u00B7-\uFFFF]+$/)
;const ARIA_ATTR=seal(/^aria-[\-\w]+$/)
;const IS_ALLOWED_URI=seal(/^(?:(?:(?:f|ht)tps?|mailto|tel|callto|sms|cid|xmpp|matrix):|[^a-z]|[a-z+.\-]+(?:[^a-z+.\-:]|$))/i)
;const IS_SCRIPT_OR_DATA=seal(/^(?:\w+script|data):/i)
;const ATTR_WHITESPACE=seal(/[\u0000-\u0020\u00A0\u1680\u180E\u2000-\u2029\u205F\u3000]/g)
;const DOCTYPE_NAME=seal(/^html$/i);const CUSTOM_ELEMENT=seal(/^[a-z][.\w]*(-[.\w]+)+$/i)
;const ELEMENT_MARKUP_PROBE=seal(/<[/\w!]/g);const COMMENT_MARKUP_PROBE=seal(/<[/\w]/g)
;const FALLBACK_TAG_CLOSE=seal(/<\/no(script|embed|frames)/i);const SELF_CLOSING_TAG=seal(/\/>/i);const NODE_TYPE={
element:1,attribute:2,text:3,cdataSection:4,entityReference:5,entityNode:6,processingInstruction:7,comment:8,document:9,
documentType:10,documentFragment:11,notation:12}
;const LITERAL_TEXT_ELEMENT_NAMES=["style","script","xmp","iframe","noembed","noframes","plaintext","noscript"]
;const LITERAL_TEXT_ELEMENTS=freeze(addToSet({},LITERAL_TEXT_ELEMENT_NAMES));const LITERAL_TEXT_CLOSE=function(){
const map={};arrayForEach(LITERAL_TEXT_ELEMENT_NAMES,name=>{
map[name]=seal(new RegExp("</"+name+"(?=[\\t\\n\\f\\r />])","i"))});return freeze(map)}()
;const getGlobal=function getGlobal(){return typeof window==="undefined"?null:window}
;const _createTrustedTypesPolicy=function _createTrustedTypesPolicy(trustedTypes,purifyHostElement){
if(typeof trustedTypes!=="object"||typeof trustedTypes.createPolicy!=="function"){return null}let suffix=null
;const ATTR_NAME="data-tt-policy-suffix";if(purifyHostElement&&purifyHostElement.hasAttribute(ATTR_NAME)){
suffix=purifyHostElement.getAttribute(ATTR_NAME)}const policyName="dompurify"+(suffix?"#"+suffix:"");try{
return trustedTypes.createPolicy(policyName,{createHTML(html){return html},createScriptURL(scriptUrl){return scriptUrl}
})}catch(_){console.warn("TrustedTypes policy "+policyName+" could not be created.");return null}}
;const _createHooksMap=function _createHooksMap(){return{afterSanitizeAttributes:[],afterSanitizeElements:[],
afterSanitizeShadowDOM:[],beforeSanitizeAttributes:[],beforeSanitizeElements:[],beforeSanitizeShadowDOM:[],
uponSanitizeAttribute:[],uponSanitizeElement:[],uponSanitizeShadowNode:[]}}
;const _resolveSetOption=function _resolveSetOption(cfg,key,fallback,options){
return objectHasOwnProperty(cfg,key)&&arrayIsArray(cfg[key])?addToSet(options.base?clone(options.base):{},cfg[key],options.transform):fallback
};const _resolveObjectOption=function _resolveObjectOption(cfg,key,makeFallback){
const value=objectHasOwnProperty(cfg,key)?cfg[key]:undefined
;return value&&typeof value==="object"?clone(value):makeFallback()};function createDOMPurify(){
let window=arguments.length>0&&arguments[0]!==undefined?arguments[0]:getGlobal()
;const DOMPurify=root=>createDOMPurify(root);DOMPurify.version="3.4.15";DOMPurify.removed=[]
;if(!window||!window.document||window.document.nodeType!==NODE_TYPE.document||!window.Element){
DOMPurify.isSupported=false;return DOMPurify}let document=window.document;const originalDocument=document
;const currentScript=originalDocument.currentScript;window.DocumentFragment
;const HTMLTemplateElement=window.HTMLTemplateElement,Node=window.Node,Element=window.Element,NodeFilter=window.NodeFilter,_window$NamedNodeMap=window.NamedNodeMap
;_window$NamedNodeMap===void 0?window.NamedNodeMap||window.MozNamedAttrMap:_window$NamedNodeMap;window.HTMLFormElement
;const DOMParser=window.DOMParser,trustedTypes=window.trustedTypes;const ElementPrototype=Element.prototype
;const cloneNode=lookupGetter(ElementPrototype,"cloneNode");const remove=lookupGetter(ElementPrototype,"remove")
;const removeAttributeNode=lookupGetter(ElementPrototype,"removeAttributeNode")
;const getNextSibling=lookupGetter(ElementPrototype,"nextSibling")
;const getChildNodes=lookupGetter(ElementPrototype,"childNodes")
;const getParentNode=lookupGetter(ElementPrototype,"parentNode")
;const getShadowRoot=lookupGetter(ElementPrototype,"shadowRoot")
;const getAttributes=lookupGetter(ElementPrototype,"attributes")
;const getNodeType=Node&&Node.prototype?lookupGetter(Node.prototype,"nodeType"):null
;const getNodeName=Node&&Node.prototype?lookupGetter(Node.prototype,"nodeName"):null
;const getOwnerDocument=Node&&Node.prototype?lookupGetter(Node.prototype,"ownerDocument"):null
;const _readNodeType=function _readNodeType(node){return getNodeType?getNodeType(node):node.nodeType}
;const _readNodeName=function _readNodeName(node){return getNodeName?getNodeName(node):node.nodeName}
;if(typeof HTMLTemplateElement==="function"){const template=document.createElement("template")
;if(template.content&&template.content.ownerDocument){document=template.content.ownerDocument}}let trustedTypesPolicy
;let emptyHTML="";let defaultTrustedTypesPolicy;let defaultTrustedTypesPolicyResolved=false
;let IN_TRUSTED_TYPES_POLICY=0;const _assertNotInTrustedTypesPolicy=function _assertNotInTrustedTypesPolicy(){
if(IN_TRUSTED_TYPES_POLICY>0){
throw typeErrorCreate("A configured TRUSTED_TYPES_POLICY callback (createHTML or "+"createScriptURL) must not call DOMPurify.sanitize, as that causes "+"infinite recursion. Do not pass a policy whose callbacks wrap "+'DOMPurify as TRUSTED_TYPES_POLICY; see the "DOMPurify and Trusted '+'Types" section of the README.')
}};const _createTrustedHTML=function _createTrustedHTML(html){_assertNotInTrustedTypesPolicy();IN_TRUSTED_TYPES_POLICY++
;try{return trustedTypesPolicy.createHTML(html)}finally{IN_TRUSTED_TYPES_POLICY--}}
;const _createTrustedScriptURL=function _createTrustedScriptURL(scriptUrl){_assertNotInTrustedTypesPolicy()
;IN_TRUSTED_TYPES_POLICY++;try{return trustedTypesPolicy.createScriptURL(scriptUrl)}finally{IN_TRUSTED_TYPES_POLICY--}}
;const _getDefaultTrustedTypesPolicy=function _getDefaultTrustedTypesPolicy(){if(!defaultTrustedTypesPolicyResolved){
defaultTrustedTypesPolicy=_createTrustedTypesPolicy(trustedTypes,currentScript);defaultTrustedTypesPolicyResolved=true}
return defaultTrustedTypesPolicy}
;const _document=document,implementation=_document.implementation,createNodeIterator=_document.createNodeIterator,createDocumentFragment=_document.createDocumentFragment,getElementsByTagName=_document.getElementsByTagName
;const importNode=originalDocument.importNode;let hooks=_createHooksMap()
;DOMPurify.isSupported=typeof entries==="function"&&typeof getParentNode==="function"&&implementation&&implementation.createHTMLDocument!==undefined
;const MUSTACHE_EXPR$1=MUSTACHE_EXPR,ERB_EXPR$1=ERB_EXPR,TMPLIT_EXPR$1=TMPLIT_EXPR,DATA_ATTR$1=DATA_ATTR,ARIA_ATTR$1=ARIA_ATTR,IS_SCRIPT_OR_DATA$1=IS_SCRIPT_OR_DATA,ATTR_WHITESPACE$1=ATTR_WHITESPACE,CUSTOM_ELEMENT$1=CUSTOM_ELEMENT
;let IS_ALLOWED_URI$1=IS_ALLOWED_URI;let ALLOWED_TAGS=null
;const DEFAULT_ALLOWED_TAGS=addToSet({},[...html$1,...svg$1,...svgFilters,...mathMl$1,...text]);let ALLOWED_ATTR=null
;const DEFAULT_ALLOWED_ATTR=addToSet({},[...html,...svg,...mathMl,...xml])
;let CUSTOM_ELEMENT_HANDLING=Object.seal(create(null,{tagNameCheck:{writable:true,configurable:false,enumerable:true,
value:null},attributeNameCheck:{writable:true,configurable:false,enumerable:true,value:null},
allowCustomizedBuiltInElements:{writable:true,configurable:false,enumerable:true,value:false}}));let FORBID_TAGS=null
;let FORBID_ATTR=null;const EXTRA_ELEMENT_HANDLING=Object.seal(create(null,{tagCheck:{writable:true,configurable:false,
enumerable:true,value:null},attributeCheck:{writable:true,configurable:false,enumerable:true,value:null}}))
;let ALLOW_ARIA_ATTR=true;let ALLOW_DATA_ATTR=true;let ALLOW_UNKNOWN_PROTOCOLS=false;let ALLOW_SELF_CLOSE_IN_ATTR=true
;let SAFE_FOR_TEMPLATES=false;let SAFE_FOR_XML=true;let WHOLE_DOCUMENT=false;let SET_CONFIG=false
;let SET_CONFIG_ALLOWED_TAGS=null;let SET_CONFIG_ALLOWED_ATTR=null;let FORCE_BODY=false;let RETURN_DOM=false
;let RETURN_DOM_FRAGMENT=false;let RETURN_TRUSTED_TYPE=false;let SANITIZE_DOM=true;let SANITIZE_NAMED_PROPS=false
;const SANITIZE_NAMED_PROPS_PREFIX="user-content-";let KEEP_CONTENT=true;let IN_PLACE=false;let USE_PROFILES={}
;let FORBID_CONTENTS=null
;const DEFAULT_FORBID_CONTENTS=addToSet({},["annotation-xml","audio","colgroup","desc","foreignobject","head","iframe","math","mi","mn","mo","ms","mtext","noembed","noframes","noscript","plaintext","script","selectedcontent","style","svg","template","thead","title","video","xmp"])
;let DATA_URI_TAGS=null;const DEFAULT_DATA_URI_TAGS=addToSet({},["audio","video","img","source","image","track"])
;let URI_SAFE_ATTRIBUTES=null
;const DEFAULT_URI_SAFE_ATTRIBUTES=addToSet({},["alt","class","for","id","label","name","pattern","placeholder","role","summary","title","value","style","xmlns"])
;const MATHML_NAMESPACE="http://www.w3.org/1998/Math/MathML";const SVG_NAMESPACE="http://www.w3.org/2000/svg"
;const HTML_NAMESPACE="http://www.w3.org/1999/xhtml";let NAMESPACE=HTML_NAMESPACE;let IS_EMPTY_INPUT=false
;let ALLOWED_NAMESPACES=null
;const DEFAULT_ALLOWED_NAMESPACES=addToSet({},[MATHML_NAMESPACE,SVG_NAMESPACE,HTML_NAMESPACE],stringToString)
;const DEFAULT_MATHML_TEXT_INTEGRATION_POINTS=freeze(["mi","mo","mn","ms","mtext"])
;let MATHML_TEXT_INTEGRATION_POINTS=addToSet({},DEFAULT_MATHML_TEXT_INTEGRATION_POINTS)
;const DEFAULT_HTML_INTEGRATION_POINTS=freeze(["annotation-xml"])
;let HTML_INTEGRATION_POINTS=addToSet({},DEFAULT_HTML_INTEGRATION_POINTS)
;const COMMON_SVG_AND_HTML_ELEMENTS=addToSet({},["title","style","font","a","script"]);let PARSER_MEDIA_TYPE=null
;const SUPPORTED_PARSER_MEDIA_TYPES=["application/xhtml+xml","text/html"];const DEFAULT_PARSER_MEDIA_TYPE="text/html"
;let transformCaseFunc=null;let CONFIG=null;const formElement=document.createElement("form")
;const isRegexOrFunction=function isRegexOrFunction(testValue){
return testValue instanceof RegExp||testValue instanceof Function};const _parseConfig=function _parseConfig(){
let cfg=arguments.length>0&&arguments[0]!==undefined?arguments[0]:{};if(CONFIG&&CONFIG===cfg){return}
if(!cfg||typeof cfg!=="object"){cfg={}}cfg=clone(cfg)
;PARSER_MEDIA_TYPE=SUPPORTED_PARSER_MEDIA_TYPES.indexOf(cfg.PARSER_MEDIA_TYPE)===-1?DEFAULT_PARSER_MEDIA_TYPE:cfg.PARSER_MEDIA_TYPE
;transformCaseFunc=PARSER_MEDIA_TYPE==="application/xhtml+xml"?stringToString:stringToLowerCase
;ALLOWED_TAGS=_resolveSetOption(cfg,"ALLOWED_TAGS",DEFAULT_ALLOWED_TAGS,{transform:transformCaseFunc})
;ALLOWED_ATTR=_resolveSetOption(cfg,"ALLOWED_ATTR",DEFAULT_ALLOWED_ATTR,{transform:transformCaseFunc})
;ALLOWED_NAMESPACES=_resolveSetOption(cfg,"ALLOWED_NAMESPACES",DEFAULT_ALLOWED_NAMESPACES,{transform:stringToString})
;URI_SAFE_ATTRIBUTES=_resolveSetOption(cfg,"ADD_URI_SAFE_ATTR",DEFAULT_URI_SAFE_ATTRIBUTES,{transform:transformCaseFunc,
base:DEFAULT_URI_SAFE_ATTRIBUTES});DATA_URI_TAGS=_resolveSetOption(cfg,"ADD_DATA_URI_TAGS",DEFAULT_DATA_URI_TAGS,{
transform:transformCaseFunc,base:DEFAULT_DATA_URI_TAGS})
;FORBID_CONTENTS=_resolveSetOption(cfg,"FORBID_CONTENTS",DEFAULT_FORBID_CONTENTS,{transform:transformCaseFunc})
;FORBID_TAGS=_resolveSetOption(cfg,"FORBID_TAGS",clone({}),{transform:transformCaseFunc})
;FORBID_ATTR=_resolveSetOption(cfg,"FORBID_ATTR",clone({}),{transform:transformCaseFunc})
;USE_PROFILES=objectHasOwnProperty(cfg,"USE_PROFILES")?cfg.USE_PROFILES&&typeof cfg.USE_PROFILES==="object"?clone(cfg.USE_PROFILES):cfg.USE_PROFILES:false
;ALLOW_ARIA_ATTR=cfg.ALLOW_ARIA_ATTR!==false;ALLOW_DATA_ATTR=cfg.ALLOW_DATA_ATTR!==false
;ALLOW_UNKNOWN_PROTOCOLS=cfg.ALLOW_UNKNOWN_PROTOCOLS||false
;ALLOW_SELF_CLOSE_IN_ATTR=cfg.ALLOW_SELF_CLOSE_IN_ATTR!==false;SAFE_FOR_TEMPLATES=cfg.SAFE_FOR_TEMPLATES||false
;SAFE_FOR_XML=cfg.SAFE_FOR_XML!==false;WHOLE_DOCUMENT=cfg.WHOLE_DOCUMENT||false;RETURN_DOM=cfg.RETURN_DOM||false
;RETURN_DOM_FRAGMENT=cfg.RETURN_DOM_FRAGMENT||false;RETURN_TRUSTED_TYPE=cfg.RETURN_TRUSTED_TYPE||false
;FORCE_BODY=cfg.FORCE_BODY||false;SANITIZE_DOM=cfg.SANITIZE_DOM!==false
;SANITIZE_NAMED_PROPS=cfg.SANITIZE_NAMED_PROPS||false;KEEP_CONTENT=cfg.KEEP_CONTENT!==false;IN_PLACE=cfg.IN_PLACE||false
;IS_ALLOWED_URI$1=isRegex(cfg.ALLOWED_URI_REGEXP)?cfg.ALLOWED_URI_REGEXP:IS_ALLOWED_URI
;NAMESPACE=typeof cfg.NAMESPACE==="string"?cfg.NAMESPACE:HTML_NAMESPACE
;MATHML_TEXT_INTEGRATION_POINTS=_resolveObjectOption(cfg,"MATHML_TEXT_INTEGRATION_POINTS",()=>addToSet({},DEFAULT_MATHML_TEXT_INTEGRATION_POINTS))
;HTML_INTEGRATION_POINTS=_resolveObjectOption(cfg,"HTML_INTEGRATION_POINTS",()=>addToSet({},DEFAULT_HTML_INTEGRATION_POINTS))
;const customElementHandling=_resolveObjectOption(cfg,"CUSTOM_ELEMENT_HANDLING",()=>create(null))
;CUSTOM_ELEMENT_HANDLING=create(null)
;if(objectHasOwnProperty(customElementHandling,"tagNameCheck")&&isRegexOrFunction(customElementHandling.tagNameCheck)){
CUSTOM_ELEMENT_HANDLING.tagNameCheck=customElementHandling.tagNameCheck}
if(objectHasOwnProperty(customElementHandling,"attributeNameCheck")&&isRegexOrFunction(customElementHandling.attributeNameCheck)){
CUSTOM_ELEMENT_HANDLING.attributeNameCheck=customElementHandling.attributeNameCheck}
if(objectHasOwnProperty(customElementHandling,"allowCustomizedBuiltInElements")&&typeof customElementHandling.allowCustomizedBuiltInElements==="boolean"){
CUSTOM_ELEMENT_HANDLING.allowCustomizedBuiltInElements=customElementHandling.allowCustomizedBuiltInElements}
seal(CUSTOM_ELEMENT_HANDLING);if(SAFE_FOR_TEMPLATES){ALLOW_DATA_ATTR=false}if(RETURN_DOM_FRAGMENT){RETURN_DOM=true}
if(USE_PROFILES){ALLOWED_TAGS=addToSet({},text);ALLOWED_ATTR=create(null);if(USE_PROFILES.html===true){
addToSet(ALLOWED_TAGS,html$1);addToSet(ALLOWED_ATTR,html)}if(USE_PROFILES.svg===true){addToSet(ALLOWED_TAGS,svg$1)
;addToSet(ALLOWED_ATTR,svg);addToSet(ALLOWED_ATTR,xml)}if(USE_PROFILES.svgFilters===true){
addToSet(ALLOWED_TAGS,svgFilters);addToSet(ALLOWED_ATTR,svg);addToSet(ALLOWED_ATTR,xml)}if(USE_PROFILES.mathMl===true){
addToSet(ALLOWED_TAGS,mathMl$1);addToSet(ALLOWED_ATTR,mathMl);addToSet(ALLOWED_ATTR,xml)}}
EXTRA_ELEMENT_HANDLING.tagCheck=null;EXTRA_ELEMENT_HANDLING.attributeCheck=null
;if(objectHasOwnProperty(cfg,"ADD_TAGS")){if(typeof cfg.ADD_TAGS==="function"){
EXTRA_ELEMENT_HANDLING.tagCheck=cfg.ADD_TAGS}else if(arrayIsArray(cfg.ADD_TAGS)){
if(ALLOWED_TAGS===DEFAULT_ALLOWED_TAGS){ALLOWED_TAGS=clone(ALLOWED_TAGS)}
addToSet(ALLOWED_TAGS,cfg.ADD_TAGS,transformCaseFunc)}}if(objectHasOwnProperty(cfg,"ADD_ATTR")){
if(typeof cfg.ADD_ATTR==="function"){EXTRA_ELEMENT_HANDLING.attributeCheck=cfg.ADD_ATTR
}else if(arrayIsArray(cfg.ADD_ATTR)){if(ALLOWED_ATTR===DEFAULT_ALLOWED_ATTR){ALLOWED_ATTR=clone(ALLOWED_ATTR)}
addToSet(ALLOWED_ATTR,cfg.ADD_ATTR,transformCaseFunc)}}
if(objectHasOwnProperty(cfg,"ADD_FORBID_CONTENTS")&&arrayIsArray(cfg.ADD_FORBID_CONTENTS)){
if(FORBID_CONTENTS===DEFAULT_FORBID_CONTENTS){FORBID_CONTENTS=clone(FORBID_CONTENTS)}
addToSet(FORBID_CONTENTS,cfg.ADD_FORBID_CONTENTS,transformCaseFunc)}if(KEEP_CONTENT){ALLOWED_TAGS["#text"]=true}
if(WHOLE_DOCUMENT){addToSet(ALLOWED_TAGS,["html","head","body"])}if(ALLOWED_TAGS.table){addToSet(ALLOWED_TAGS,["tbody"])
;delete FORBID_TAGS.tbody}if(cfg.TRUSTED_TYPES_POLICY){if(typeof cfg.TRUSTED_TYPES_POLICY.createHTML!=="function"){
throw typeErrorCreate('TRUSTED_TYPES_POLICY configuration option must provide a "createHTML" hook.')}
if(typeof cfg.TRUSTED_TYPES_POLICY.createScriptURL!=="function"){
throw typeErrorCreate('TRUSTED_TYPES_POLICY configuration option must provide a "createScriptURL" hook.')}
const previousTrustedTypesPolicy=trustedTypesPolicy;trustedTypesPolicy=cfg.TRUSTED_TYPES_POLICY;try{
emptyHTML=_createTrustedHTML("")}catch(error){trustedTypesPolicy=previousTrustedTypesPolicy;throw error}
}else if(cfg.TRUSTED_TYPES_POLICY===null){trustedTypesPolicy=undefined;emptyHTML=""}else{
if(trustedTypesPolicy===undefined){trustedTypesPolicy=_getDefaultTrustedTypesPolicy()}
if(trustedTypesPolicy&&typeof emptyHTML==="string"){emptyHTML=_createTrustedHTML("")}}if(freeze){freeze(cfg)}CONFIG=cfg}
;const ALL_SVG_TAGS=addToSet({},[...svg$1,...svgFilters,...svgDisallowed])
;const ALL_MATHML_TAGS=addToSet({},[...mathMl$1,...mathMlDisallowed])
;const _checkSvgNamespace=function _checkSvgNamespace(tagName,parent,parentTagName){
if(parent.namespaceURI===HTML_NAMESPACE){return tagName==="svg"}if(parent.namespaceURI===MATHML_NAMESPACE){
return tagName==="svg"&&(parentTagName==="annotation-xml"||MATHML_TEXT_INTEGRATION_POINTS[parentTagName])}
return Boolean(ALL_SVG_TAGS[tagName])}
;const _checkMathMlNamespace=function _checkMathMlNamespace(tagName,parent,parentTagName){
if(parent.namespaceURI===HTML_NAMESPACE){return tagName==="math"}if(parent.namespaceURI===SVG_NAMESPACE){
return tagName==="math"&&HTML_INTEGRATION_POINTS[parentTagName]}return Boolean(ALL_MATHML_TAGS[tagName])}
;const _checkHtmlNamespace=function _checkHtmlNamespace(tagName,parent,parentTagName){
if(parent.namespaceURI===SVG_NAMESPACE&&!HTML_INTEGRATION_POINTS[parentTagName]){return false}
if(parent.namespaceURI===MATHML_NAMESPACE&&!MATHML_TEXT_INTEGRATION_POINTS[parentTagName]){return false}
return!ALL_MATHML_TAGS[tagName]&&(COMMON_SVG_AND_HTML_ELEMENTS[tagName]||!ALL_SVG_TAGS[tagName])}
;const _checkValidNamespace=function _checkValidNamespace(element){let parent=getParentNode(element)
;if(!parent||!parent.tagName){parent={namespaceURI:NAMESPACE,tagName:"template"}}
const tagName=stringToLowerCase(element.tagName);const parentTagName=stringToLowerCase(parent.tagName)
;if(!ALLOWED_NAMESPACES[element.namespaceURI]){return false}if(element.namespaceURI===SVG_NAMESPACE){
return _checkSvgNamespace(tagName,parent,parentTagName)}if(element.namespaceURI===MATHML_NAMESPACE){
return _checkMathMlNamespace(tagName,parent,parentTagName)}if(element.namespaceURI===HTML_NAMESPACE){
return _checkHtmlNamespace(tagName,parent,parentTagName)}
if(PARSER_MEDIA_TYPE==="application/xhtml+xml"&&ALLOWED_NAMESPACES[element.namespaceURI]){return true}return false}
;const _forceRemove=function _forceRemove(node){arrayPush(DOMPurify.removed,{element:node});try{
getParentNode(node).removeChild(node)}catch(_){remove(node);if(!getParentNode(node)){
throw typeErrorCreate("a node selected for removal could not be detached from its tree "+"and cannot be safely returned; refusing to sanitize in place")
}}};const _stripAttributeNode=function _stripAttributeNode(element,attribute,name){try{
removeAttributeNode(element,attribute)}catch(_){try{element.removeAttribute(name)}catch(_){}}}
;const _neutralizeRoot=function _neutralizeRoot(root){_neutralizeSubtree(root);const childNodes=getChildNodes(root)
;if(childNodes){const snapshot=[];arrayForEach(childNodes,child=>{arrayPush(snapshot,child)})
;arrayForEach(snapshot,child=>{try{remove(child)}catch(_){}})}const attributes=getAttributes(root);if(attributes){
for(let i=attributes.length-1;i>=0;--i){const attribute=attributes[i];const name=attribute&&attribute.name
;if(typeof name==="string"){_stripAttributeNode(root,attribute,name)}}}}
;const _removeAttribute=function _removeAttribute(name,element,attr){if(!attr){try{attr=element.getAttributeNode(name)
}catch(_){attr=null}}arrayPush(DOMPurify.removed,{attribute:attr||null,from:element});try{if(attr){
removeAttributeNode(element,attr)}else{element.removeAttribute(name)}}catch(_){try{element.removeAttribute(name)
}catch(_){}}if(name==="is"){if(RETURN_DOM||RETURN_DOM_FRAGMENT){try{_forceRemove(element)}catch(_){}}else{try{
element.setAttribute(name,"")}catch(_){}}}}
;const _stripDisallowedAttributes=function _stripDisallowedAttributes(element){const attributes=getAttributes(element)
;if(!attributes){return}for(let i=attributes.length-1;i>=0;--i){const attribute=attributes[i]
;const name=attribute&&attribute.name;if(typeof name!=="string"||ALLOWED_ATTR[transformCaseFunc(name)]){continue}
_stripAttributeNode(element,attribute,name)}};const _neutralizeSubtree=function _neutralizeSubtree(root){
const stack=[root];while(stack.length>0){const node=stack.pop();const nodeType=_readNodeType(node)
;if(nodeType===NODE_TYPE.element){_stripDisallowedAttributes(node)}const childNodes=getChildNodes(node);if(childNodes){
for(let i=childNodes.length-1;i>=0;--i){stack.push(childNodes[i])}}}}
;const _isPatchLinkageAttribute=function _isPatchLinkageAttribute(lcName,lcTag){if(!SAFE_FOR_XML){return false}
if(lcName==="patchsrc"){return true}return lcName==="for"&&lcTag!=="label"&&lcTag!=="output"}
;const _neutralizePatchLinkage=function _neutralizePatchLinkage(root){if(!SAFE_FOR_XML){return}const stack=[root]
;while(stack.length>0){const node=stack.pop();const nodeType=_readNodeType(node)
;if(nodeType===NODE_TYPE.processingInstruction||nodeType===NODE_TYPE.comment&&regExpTest(COMMENT_MARKUP_PROBE,node.data)){
try{remove(node)}catch(_){}continue}if(nodeType===NODE_TYPE.element){const element=node
;const lcTag=transformCaseFunc(_readNodeName(node));try{if(element.hasAttribute&&element.hasAttribute("patchsrc")){
element.removeAttribute("patchsrc")}
if(element.hasAttribute&&element.hasAttribute("for")&&_isPatchLinkageAttribute("for",lcTag)){
element.removeAttribute("for")}}catch(_){}}const childNodes=getChildNodes(node);if(childNodes){
for(let i=childNodes.length-1;i>=0;--i){stack.push(childNodes[i])}}}};const _initDocument=function _initDocument(dirty){
let doc=null;let leadingWhitespace=null;if(FORCE_BODY){dirty="<remove></remove>"+dirty}else{
const matches=stringMatch(dirty,/^[\r\n\t ]+/);leadingWhitespace=matches&&matches[0]}
if(PARSER_MEDIA_TYPE==="application/xhtml+xml"&&NAMESPACE===HTML_NAMESPACE){
dirty='<html xmlns="http://www.w3.org/1999/xhtml"><head></head><body>'+dirty+"</body></html>"}
const dirtyPayload=trustedTypesPolicy?_createTrustedHTML(dirty):dirty;if(NAMESPACE===HTML_NAMESPACE){try{
doc=(new DOMParser).parseFromString(dirtyPayload,PARSER_MEDIA_TYPE)}catch(_){}}if(!doc||!doc.documentElement){
doc=implementation.createDocument(NAMESPACE,"template",null);try{
doc.documentElement.innerHTML=IS_EMPTY_INPUT?emptyHTML:dirtyPayload}catch(_){}}const body=doc.body||doc.documentElement
;if(dirty&&leadingWhitespace){body.insertBefore(document.createTextNode(leadingWhitespace),body.childNodes[0]||null)}
if(NAMESPACE===HTML_NAMESPACE){return getElementsByTagName.call(doc,WHOLE_DOCUMENT?"html":"body")[0]}
return WHOLE_DOCUMENT?doc.documentElement:body};const _createNodeIterator=function _createNodeIterator(root){
const doc=getOwnerDocument?getOwnerDocument(root):root.ownerDocument
;return createNodeIterator.call(doc||root,root,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_COMMENT|NodeFilter.SHOW_TEXT|NodeFilter.SHOW_PROCESSING_INSTRUCTION|NodeFilter.SHOW_CDATA_SECTION,null)
};const _stripTemplateExpressions=function _stripTemplateExpressions(value){
value=stringReplace(value,MUSTACHE_EXPR$1," ");value=stringReplace(value,ERB_EXPR$1," ")
;value=stringReplace(value,TMPLIT_EXPR$1," ");return value}
;const _scrubTemplateExpressions2=function _scrubTemplateExpressions(node){var _node$querySelectorAl;node.normalize()
;const doc=getOwnerDocument?getOwnerDocument(node):node.ownerDocument
;const walker=createNodeIterator.call(doc||node,node,NodeFilter.SHOW_TEXT|NodeFilter.SHOW_COMMENT|NodeFilter.SHOW_CDATA_SECTION|NodeFilter.SHOW_PROCESSING_INSTRUCTION,null)
;let currentNode=walker.nextNode();while(currentNode){currentNode.data=_stripTemplateExpressions(currentNode.data)
;currentNode=walker.nextNode()}
const templates=(_node$querySelectorAl=node.querySelectorAll)===null||_node$querySelectorAl===void 0?void 0:_node$querySelectorAl.call(node,"template")
;if(templates){arrayForEach(templates,tmpl=>{if(_isDocumentFragment(tmpl.content)){
_scrubTemplateExpressions2(tmpl.content)}})}};const _isClobbered=function _isClobbered(element){
const realTagName=getNodeName?getNodeName(element):null;if(typeof realTagName!=="string"){return false}
if(transformCaseFunc(realTagName)!=="form"){return false}
return typeof element.nodeName!=="string"||typeof element.textContent!=="string"||typeof element.removeChild!=="function"||element.attributes!==getAttributes(element)||typeof element.removeAttribute!=="function"||typeof element.removeAttributeNode!=="function"||typeof element.getAttributeNode!=="function"||typeof element.setAttribute!=="function"||typeof element.namespaceURI!=="string"||typeof element.insertBefore!=="function"||typeof element.hasChildNodes!=="function"||element.nodeType!==getNodeType(element)||element.childNodes!==getChildNodes(element)
};const _isDocumentFragment=function _isDocumentFragment(value){if(!getNodeType||typeof value!=="object"||value===null){
return false}try{return getNodeType(value)===NODE_TYPE.documentFragment}catch(_){return false}}
;const _isNode=function _isNode(value){if(!getNodeType||typeof value!=="object"||value===null){return false}try{
return typeof getNodeType(value)==="number"}catch(_){return false}};function _executeHooks(hooks,currentNode,data){
if(hooks.length===0){return}arrayForEach(hooks,hook=>{hook.call(DOMPurify,currentNode,data,CONFIG)})}
const _isUnsafeNode=function _isUnsafeNode(currentNode,tagName){
if(SAFE_FOR_XML&&currentNode.hasChildNodes()&&!_isNode(currentNode.firstElementChild)&&regExpTest(ELEMENT_MARKUP_PROBE,currentNode.textContent)&&regExpTest(ELEMENT_MARKUP_PROBE,currentNode.innerHTML)){
return true}
if(SAFE_FOR_XML&&currentNode.namespaceURI===HTML_NAMESPACE&&LITERAL_TEXT_ELEMENTS[tagName]&&(_isNode(currentNode.firstElementChild)||typeof currentNode.textContent==="string"&&regExpTest(LITERAL_TEXT_CLOSE[tagName],currentNode.textContent))){
return true}if(currentNode.nodeType===NODE_TYPE.processingInstruction){return true}
if(SAFE_FOR_XML&&currentNode.nodeType===NODE_TYPE.comment&&regExpTest(COMMENT_MARKUP_PROBE,currentNode.data)){
return true}return false};const _matchesNameCheck=function _matchesNameCheck(check,name){if(check instanceof RegExp){
return regExpTest(check,name)}if(check instanceof Function){
for(var _len=arguments.length,args=new Array(_len>2?_len-2:0),_key=2;_key<_len;_key++){args[_key-2]=arguments[_key]}
return Boolean(check(name,...args))}return false}
;const _sanitizeDisallowedNode=function _sanitizeDisallowedNode(currentNode,tagName,root){
if(!FORBID_TAGS[tagName]&&_isBasicCustomElement(tagName)&&_matchesNameCheck(CUSTOM_ELEMENT_HANDLING.tagNameCheck,tagName)){
return false}if(KEEP_CONTENT&&!FORBID_CONTENTS[tagName]){const parentNode=getParentNode(currentNode)
;const childNodes=getChildNodes(currentNode);if(childNodes&&parentNode){const childCount=childNodes.length
;for(let i=childCount-1;i>=0;--i){const hoisted=currentNode===root?cloneNode(childNodes[i],true):childNodes[i]
;parentNode.insertBefore(hoisted,getNextSibling(currentNode))}}}_forceRemove(currentNode);return true}
;const _forkSharedAllowlist=function _forkSharedAllowlist(hookList,set,defaultSet,setConfigSet){if(hookList.length===0){
return set}return set===defaultSet||set===setConfigSet?clone(set):set}
;const _handleHookDetachedNode=function _handleHookDetachedNode(currentNode,root){
if(currentNode===root||getParentNode(currentNode)!==null){return false}if(IN_PLACE){_neutralizeSubtree(currentNode)}
return true};const _sanitizeElements=function _sanitizeElements(currentNode,root){
_executeHooks(hooks.beforeSanitizeElements,currentNode,null);if(_handleHookDetachedNode(currentNode,root)){return true}
if(_isClobbered(currentNode)){_forceRemove(currentNode);return true}
const tagName=transformCaseFunc(_readNodeName(currentNode))
;ALLOWED_TAGS=_forkSharedAllowlist(hooks.uponSanitizeElement,ALLOWED_TAGS,DEFAULT_ALLOWED_TAGS,SET_CONFIG_ALLOWED_TAGS)
;_executeHooks(hooks.uponSanitizeElement,currentNode,{tagName:tagName,allowedTags:ALLOWED_TAGS})
;if(_handleHookDetachedNode(currentNode,root)){return true}if(_isUnsafeNode(currentNode,tagName)){
_forceRemove(currentNode);return true}
if(FORBID_TAGS[tagName]||!(EXTRA_ELEMENT_HANDLING.tagCheck instanceof Function&&EXTRA_ELEMENT_HANDLING.tagCheck(tagName))&&!ALLOWED_TAGS[tagName]){
const removed=_sanitizeDisallowedNode(currentNode,tagName,root);if(removed===false){
_executeHooks(hooks.afterSanitizeElements,currentNode,null)}return removed}const nt=_readNodeType(currentNode)
;if(nt===NODE_TYPE.element&&!_checkValidNamespace(currentNode)){_forceRemove(currentNode);return true}
if((tagName==="noscript"||tagName==="noembed"||tagName==="noframes")&&regExpTest(FALLBACK_TAG_CLOSE,currentNode.innerHTML)){
_forceRemove(currentNode);return true}if(SAFE_FOR_TEMPLATES&&currentNode.nodeType===NODE_TYPE.text){
const content=_stripTemplateExpressions(currentNode.textContent);if(currentNode.textContent!==content){
arrayPush(DOMPurify.removed,{element:currentNode.cloneNode()});currentNode.textContent=content}}
_executeHooks(hooks.afterSanitizeElements,currentNode,null);return false}
;const _isValidAttribute=function _isValidAttribute(lcTag,lcName,value){if(FORBID_ATTR[lcName]){return false}
if(_isPatchLinkageAttribute(lcName,lcTag)){return false}
if(SANITIZE_DOM&&(lcName==="id"||lcName==="name")&&(value in document||value in formElement)){return false}
const nameIsPermitted=ALLOWED_ATTR[lcName]||EXTRA_ELEMENT_HANDLING.attributeCheck instanceof Function&&EXTRA_ELEMENT_HANDLING.attributeCheck(lcName,lcTag)
;if(ALLOW_DATA_ATTR&&regExpTest(DATA_ATTR$1,lcName)){return true}if(ALLOW_ARIA_ATTR&&regExpTest(ARIA_ATTR$1,lcName)){
return true}if(!nameIsPermitted){
return _isBasicCustomElement(lcTag)&&_matchesNameCheck(CUSTOM_ELEMENT_HANDLING.tagNameCheck,lcTag)&&_matchesNameCheck(CUSTOM_ELEMENT_HANDLING.attributeNameCheck,lcName,lcTag)||lcName==="is"&&CUSTOM_ELEMENT_HANDLING.allowCustomizedBuiltInElements&&_matchesNameCheck(CUSTOM_ELEMENT_HANDLING.tagNameCheck,value)
}if(URI_SAFE_ATTRIBUTES[lcName]){return true}if(regExpTest(IS_ALLOWED_URI$1,stringReplace(value,ATTR_WHITESPACE$1,""))){
return true}
if((lcName==="src"||lcName==="xlink:href"||lcName==="href")&&lcTag!=="script"&&stringIndexOf(value,"data:")===0&&DATA_URI_TAGS[lcTag]){
return true}if(ALLOW_UNKNOWN_PROTOCOLS&&!regExpTest(IS_SCRIPT_OR_DATA$1,stringReplace(value,ATTR_WHITESPACE$1,""))){
return true}return!value}
;const RESERVED_CUSTOM_ELEMENT_NAMES=addToSet({},["annotation-xml","color-profile","font-face","font-face-format","font-face-name","font-face-src","font-face-uri","missing-glyph"])
;const _isBasicCustomElement=function _isBasicCustomElement(tagName){
return!RESERVED_CUSTOM_ELEMENT_NAMES[stringToLowerCase(tagName)]&&regExpTest(CUSTOM_ELEMENT$1,tagName)}
;const _applyTrustedTypesToAttribute=function _applyTrustedTypesToAttribute(lcTag,lcName,namespaceURI,value){
if(trustedTypesPolicy&&typeof trustedTypes==="object"&&typeof trustedTypes.getAttributeType==="function"&&!namespaceURI){
switch(trustedTypes.getAttributeType(lcTag,lcName)){case"TrustedHTML":{return _createTrustedHTML(value)}
case"TrustedScriptURL":{return _createTrustedScriptURL(value)}}}return value}
;const _setAttributeValue=function _setAttributeValue(currentNode,name,namespaceURI,value){try{if(namespaceURI){
currentNode.setAttributeNS(namespaceURI,name,value)}else{currentNode.setAttribute(name,value)}
if(_isClobbered(currentNode)){_forceRemove(currentNode);return false}return true}catch(_){
_removeAttribute(name,currentNode);return false}};const _sanitizeAttributes=function _sanitizeAttributes(currentNode){
_executeHooks(hooks.beforeSanitizeAttributes,currentNode,null);const attributes=currentNode.attributes
;if(!attributes||_isClobbered(currentNode)){return}
ALLOWED_ATTR=_forkSharedAllowlist(hooks.uponSanitizeAttribute,ALLOWED_ATTR,DEFAULT_ALLOWED_ATTR,SET_CONFIG_ALLOWED_ATTR)
;const hookEvent={attrName:"",attrValue:"",keepAttr:true,allowedAttributes:ALLOWED_ATTR,forceKeepAttr:undefined}
;let l=attributes.length;const lcTag=transformCaseFunc(currentNode.nodeName);while(l--){const attr=attributes[l]
;const name=attr.name,namespaceURI=attr.namespaceURI,attrValue=attr.value;const lcName=transformCaseFunc(name)
;const initValue=attrValue;let value=name==="value"?initValue:stringTrim(initValue);let recreatedNamedProp=false
;hookEvent.attrName=lcName;hookEvent.attrValue=value;hookEvent.keepAttr=true;hookEvent.forceKeepAttr=undefined
;_executeHooks(hooks.uponSanitizeAttribute,currentNode,hookEvent);value=hookEvent.attrValue
;if(SANITIZE_NAMED_PROPS&&(lcName==="id"||lcName==="name")&&stringIndexOf(value,SANITIZE_NAMED_PROPS_PREFIX)!==0){
_removeAttribute(name,currentNode,attr);value=SANITIZE_NAMED_PROPS_PREFIX+value;recreatedNamedProp=true}
if(SAFE_FOR_XML&&regExpTest(/((--!?|])>)|<\/(style|script|title|xmp|textarea|noscript|iframe|noembed|noframes)/i,value)){
_removeAttribute(name,currentNode,attr);continue}if(lcName==="attributename"&&stringMatch(value,"href")){
_removeAttribute(name,currentNode,attr);continue}if(hookEvent.forceKeepAttr){continue}if(!hookEvent.keepAttr){
_removeAttribute(name,currentNode,attr);continue}if(!ALLOW_SELF_CLOSE_IN_ATTR&&regExpTest(SELF_CLOSING_TAG,value)){
_removeAttribute(name,currentNode,attr);continue}if(SAFE_FOR_TEMPLATES){value=_stripTemplateExpressions(value)}
if(!_isValidAttribute(lcTag,lcName,value)){_removeAttribute(name,currentNode,attr);continue}
value=_applyTrustedTypesToAttribute(lcTag,lcName,namespaceURI,value);if(value!==initValue){
const cleanWrite=_setAttributeValue(currentNode,name,namespaceURI,value);if(cleanWrite&&recreatedNamedProp){
arrayPop(DOMPurify.removed)}}}_executeHooks(hooks.afterSanitizeAttributes,currentNode,null)}
;const _sanitizeShadowDOM2=function _sanitizeShadowDOM(fragment){let shadowNode=null
;const shadowIterator=_createNodeIterator(fragment);_executeHooks(hooks.beforeSanitizeShadowDOM,fragment,null)
;while(shadowNode=shadowIterator.nextNode()){_executeHooks(hooks.uponSanitizeShadowNode,shadowNode,null)
;_sanitizeElements(shadowNode,fragment);_sanitizeAttributes(shadowNode);if(_isDocumentFragment(shadowNode.content)){
_sanitizeShadowDOM2(shadowNode.content)}if(_readNodeType(shadowNode)===NODE_TYPE.element){
const innerSr=getShadowRoot(shadowNode);if(_isDocumentFragment(innerSr)){_sanitizeAttachedShadowRoots(innerSr)
;_sanitizeShadowDOM2(innerSr)}}}_executeHooks(hooks.afterSanitizeShadowDOM,fragment,null)}
;const _sanitizeAttachedShadowRoots=function _sanitizeAttachedShadowRoots(root){const stack=[{node:root,shadow:null}]
;while(stack.length>0){const item=stack.pop();if(item.shadow){_sanitizeShadowDOM2(item.shadow);continue}
const node=item.node;const nodeType=_readNodeType(node);const isElement=nodeType===NODE_TYPE.element
;const childNodes=getChildNodes(node);if(childNodes){for(let i=childNodes.length-1;i>=0;--i){stack.push({
node:childNodes[i],shadow:null})}}if(isElement){const rootName=getNodeName?getNodeName(node):null
;if(typeof rootName==="string"&&transformCaseFunc(rootName)==="template"){const content=node.content
;if(_isDocumentFragment(content)){stack.push({node:content,shadow:null})}}}if(isElement){const sr=getShadowRoot(node)
;if(_isDocumentFragment(sr)){stack.push({node:null,shadow:sr},{node:sr,shadow:null})}}}}
;DOMPurify.sanitize=function(dirty){let cfg=arguments.length>1&&arguments[1]!==undefined?arguments[1]:{};let body=null
;let importedNode=null;let currentNode=null;let returnNode=null;IS_EMPTY_INPUT=!dirty;if(IS_EMPTY_INPUT){
dirty="\x3c!--\x3e"}if(typeof dirty!=="string"&&!_isNode(dirty)){dirty=stringifyValue(dirty)
;if(typeof dirty!=="string"){throw typeErrorCreate("dirty is not a string, aborting")}}if(!DOMPurify.isSupported){
return dirty}if(SET_CONFIG){ALLOWED_TAGS=SET_CONFIG_ALLOWED_TAGS;ALLOWED_ATTR=SET_CONFIG_ALLOWED_ATTR}else{
_parseConfig(cfg)}if(hooks.uponSanitizeElement.length>0||hooks.uponSanitizeAttribute.length>0){
ALLOWED_TAGS=clone(ALLOWED_TAGS)}if(hooks.uponSanitizeAttribute.length>0){ALLOWED_ATTR=clone(ALLOWED_ATTR)}
DOMPurify.removed=[];const inPlace=IN_PLACE&&typeof dirty!=="string"&&_isNode(dirty);if(inPlace){
_neutralizePatchLinkage(dirty);const nn=_readNodeName(dirty);if(typeof nn==="string"){
const tagName=transformCaseFunc(nn);if(!ALLOWED_TAGS[tagName]||FORBID_TAGS[tagName]){_neutralizeRoot(dirty)
;throw typeErrorCreate("root node is forbidden and cannot be sanitized in-place")}}if(_isClobbered(dirty)){
_neutralizeRoot(dirty);throw typeErrorCreate("root node is clobbered and cannot be sanitized in-place")}try{
_sanitizeAttachedShadowRoots(dirty)}catch(error){_neutralizeRoot(dirty);throw error}}else if(_isNode(dirty)){
body=_initDocument("\x3c!----\x3e");importedNode=body.ownerDocument.importNode(dirty,true)
;if(importedNode.nodeType===NODE_TYPE.element&&importedNode.nodeName==="BODY"){body=importedNode
}else if(importedNode.nodeName==="HTML"){body=importedNode}else{body.appendChild(importedNode)}
_sanitizeAttachedShadowRoots(body)}else{if(!RETURN_DOM&&!SAFE_FOR_TEMPLATES&&!WHOLE_DOCUMENT&&dirty.indexOf("<")===-1){
return trustedTypesPolicy&&RETURN_TRUSTED_TYPE?_createTrustedHTML(dirty):dirty}body=_initDocument(dirty);if(!body){
return RETURN_DOM?null:RETURN_TRUSTED_TYPE?emptyHTML:""}}if(body&&FORCE_BODY){_forceRemove(body.firstChild)}
const walkRoot=inPlace?dirty:body;try{const nodeIterator=_createNodeIterator(walkRoot)
;while(currentNode=nodeIterator.nextNode()){_sanitizeElements(currentNode,walkRoot);_sanitizeAttributes(currentNode)
;if(_isDocumentFragment(currentNode.content)){_sanitizeShadowDOM2(currentNode.content)}}}catch(error){if(inPlace){
_neutralizeRoot(dirty);arrayForEach(DOMPurify.removed,entry=>{if(entry.element){_neutralizeSubtree(entry.element)}})}
throw error}if(inPlace){arrayForEach(DOMPurify.removed,entry=>{if(entry.element){_neutralizeSubtree(entry.element)}})
;if(SAFE_FOR_TEMPLATES){_scrubTemplateExpressions2(dirty)}return dirty}if(RETURN_DOM){if(SAFE_FOR_TEMPLATES){
_scrubTemplateExpressions2(body)}if(RETURN_DOM_FRAGMENT){returnNode=createDocumentFragment.call(body.ownerDocument)
;while(body.firstChild){returnNode.appendChild(body.firstChild)}}else{returnNode=body}
if(ALLOWED_ATTR.shadowroot||ALLOWED_ATTR.shadowrootmode){returnNode=importNode.call(originalDocument,returnNode,true)}
return returnNode}let serializedHTML=WHOLE_DOCUMENT?body.outerHTML:body.innerHTML
;if(WHOLE_DOCUMENT&&ALLOWED_TAGS["!doctype"]&&body.ownerDocument&&body.ownerDocument.doctype&&body.ownerDocument.doctype.name&&regExpTest(DOCTYPE_NAME,body.ownerDocument.doctype.name)){
serializedHTML="<!DOCTYPE "+body.ownerDocument.doctype.name+">\n"+serializedHTML}if(SAFE_FOR_TEMPLATES){
serializedHTML=_stripTemplateExpressions(serializedHTML)}
return trustedTypesPolicy&&RETURN_TRUSTED_TYPE?_createTrustedHTML(serializedHTML):serializedHTML}
;DOMPurify.setConfig=function(){let cfg=arguments.length>0&&arguments[0]!==undefined?arguments[0]:{};_parseConfig(cfg)
;SET_CONFIG=true;SET_CONFIG_ALLOWED_TAGS=ALLOWED_TAGS;SET_CONFIG_ALLOWED_ATTR=ALLOWED_ATTR}
;DOMPurify.clearConfig=function(){CONFIG=null;SET_CONFIG=false;SET_CONFIG_ALLOWED_TAGS=null;SET_CONFIG_ALLOWED_ATTR=null
;trustedTypesPolicy=defaultTrustedTypesPolicy;emptyHTML=""};DOMPurify.isValidAttribute=function(tag,attr,value){
if(!CONFIG){_parseConfig({})}const lcTag=transformCaseFunc(tag);const lcName=transformCaseFunc(attr)
;return _isValidAttribute(lcTag,lcName,value)};DOMPurify.addHook=function(entryPoint,hookFunction){
if(typeof hookFunction!=="function"){return}if(!objectHasOwnProperty(hooks,entryPoint)){return}
arrayPush(hooks[entryPoint],hookFunction)};DOMPurify.removeHook=function(entryPoint,hookFunction){
if(!objectHasOwnProperty(hooks,entryPoint)){return undefined}if(hookFunction!==undefined){
const index=arrayLastIndexOf(hooks[entryPoint],hookFunction)
;return index===-1?undefined:arraySplice(hooks[entryPoint],index,1)[0]}return arrayPop(hooks[entryPoint])}
;DOMPurify.removeHooks=function(entryPoint){if(!objectHasOwnProperty(hooks,entryPoint)){return}hooks[entryPoint]=[]}
;DOMPurify.removeAllHooks=function(){hooks=_createHooksMap()};return DOMPurify}var purify=createDOMPurify()
;export{purify as default};
//# sourceMappingURL=purify.js.map
